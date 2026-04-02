#!/usr/bin/env python3
"""LightCycler 480 II - Connect RPC Server

Wraps the LightCycler TCP protocol with a Connect RPC API, SQLite logging,
and SwitchBot Fingerbot door control.

Usage:
    python server.py --host 192.168.95.41
    python server.py --mock
    python server.py --host 192.168.95.41 --fingerbot-mac AA:BB:CC:DD:EE:FF
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'generated'))

import lightcycler_pb2 as pb
from lightcycler_connect import LightCyclerService, LightCyclerServiceASGIApplication
from connectrpc.request import RequestContext

from lightcycler import (
    LightCyclerConnection, ResultData, MSG_SUBSYS_STATUS, MSG_STATUS_QUERY,
    MSG_QUERY_LOAD_STATE, MSG_SET_PARAMETER,
    MSG_GET_RESULT_DATA, MSG_ACK_RESULT, MSG_QUERY_RESULT_INFO,
)
from experiment import ExperimentConfig, Program, Acquisition, build_commands

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    schema_path = os.path.join(os.path.dirname(__file__), 'schema.sql')
    with open(schema_path) as f:
        db.executescript(f.read())
    return db


# ---------------------------------------------------------------------------
# LightCycler Manager
# ---------------------------------------------------------------------------

class LightCyclerManager:
    """Manages the LC connection, event logging, and experiment execution."""

    def __init__(self, host: str, port: int, db: sqlite3.Connection,
                 mock: bool = False, mock_speed: float = 50.0):
        self.host = host
        self.port = port
        self.db = db
        self._db_lock = threading.Lock()
        self.conn: LightCyclerConnection | None = None
        self.mock = mock
        self.mock_speed = mock_speed
        self._mock_server = None
        self._current_guid: str = ''
        self._running_lock = threading.Lock()

    def connect(self):
        if self.mock:
            from mock_lc import MockLightCycler
            self._mock_server = MockLightCycler(port=self.port, speed=self.mock_speed)
            self._mock_server.start()
            threading.Thread(target=self._mock_server.serve_one, daemon=True).start()
            time.sleep(0.3)

        self.conn = LightCyclerConnection(self.host, self.port)
        self.conn.connect()

        # Start event logger thread
        threading.Thread(target=self._event_logger, daemon=True).start()
        log.info("LightCycler connected, event logger started")

    def disconnect(self):
        if self.conn:
            self.conn.disconnect()
        if self._mock_server:
            self._mock_server.stop()

    def get_state(self) -> dict:
        if not self.conn or not self.conn.info:
            return {'connected': False}

        state = 0
        try:
            resp = self.conn.command(MSG_SUBSYS_STATUS, b'0', timeout=3)
            state = int(resp.fields[0])
        except Exception:
            pass

        return {
            'connected': True,
            'instrument_state': state,
            'firmware_version': self.conn.info.firmware_version,
            'controller_count': self.conn.info.controller_count,
            'current_experiment_guid': self._current_guid,
        }

    def run_experiment(self, temp_c: float, hold_time_s: float,
                       num_acquisitions: int, filter_set: int,
                       well_count: int, volume_ul: int) -> str:
        with self._running_lock:
            if self._current_guid:
                raise RuntimeError(f"Experiment {self._current_guid} already running")

        config = ExperimentConfig(
            well_count=well_count,
            volume=volume_ul,
            programs=[Program(
                cycles=1,
                acquisitions=[
                    Acquisition(
                        target_temp=temp_c,
                        hold_time=hold_time_s,
                        filter_set=filter_set,
                    )
                    for _ in range(num_acquisitions)
                ],
            )],
        )

        # Store in DB
        now = datetime.now(timezone.utc).isoformat()
        config_json = json.dumps({
            'temp_c': temp_c, 'hold_time_s': hold_time_s,
            'num_acquisitions': num_acquisitions, 'filter_set': filter_set,
            'well_count': well_count, 'volume_ul': volume_ul,
        })
        with self._db_lock:
            self.db.execute(
                'INSERT INTO experiments (guid, status, config, created_at) VALUES (?, ?, ?, ?)',
                (config.guid, 'pending', config_json, now))
            self.db.commit()

        self._current_guid = config.guid
        threading.Thread(target=self._run_experiment_thread,
                         args=(config,), daemon=True).start()
        return config.guid

    def _run_experiment_thread(self, config: ExperimentConfig):
        guid = config.guid
        try:
            self._update_experiment(guid, 'running')
            commands = build_commands(config)
            num_acq = sum(len(p.acquisitions) for p in config.programs)

            # Drain buffered events
            time.sleep(2)
            with self.conn._events_lock:
                self.conn._events.clear()

            # Pre-experiment queries
            self.conn.query(MSG_QUERY_LOAD_STATE)
            self.conn.command(MSG_SET_PARAMETER, b'15')
            self.conn.query(MSG_STATUS_QUERY)

            # Send experiment commands
            for msg_type, payload in commands:
                resp = self.conn.command(msg_type, payload)
                if resp.fields[0] != '0':
                    raise RuntimeError(f"Command 0x{msg_type:04X} failed: {resp.fields}")

            # Wait for run start
            self.conn.wait_for_event(
                lambda f: len(f) > 2 and f[2] == '104', timeout=30)
            self.conn.wait_for_event(
                lambda f: (len(f) > 4 and f[2] == '90'
                           and f[3].strip() == '12' and f[4].strip() == '100'),
                timeout=120)

            # Collect acquisitions
            for i in range(1, num_acq + 1):
                self.conn.wait_for_event(
                    lambda f: len(f) > 2 and f[2] == '201', timeout=60)
                resp = self.conn.command(
                    MSG_GET_RESULT_DATA, f"{i}\n".encode('ascii'))
                result = ResultData(resp.fields)
                self.conn.query(MSG_QUERY_RESULT_INFO)
                self.conn.command(MSG_ACK_RESULT, str(i).encode('ascii'))

                now = datetime.now(timezone.utc).isoformat()
                with self._db_lock:
                    self.db.execute(
                        'INSERT INTO acquisitions (experiment_guid, acquisition_num, '
                        'temperature_c, time_s, ref_channel, well_data, created_at) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (guid, result.acquisition, result.temperature,
                         result.time_seconds, result.ref_channel,
                         json.dumps(result.values), now))
                    self.db.commit()
                log.info("Acq %d/%d: T=%.2f°C mean=%.1f",
                         i, num_acq, result.temperature,
                         sum(result.values) / len(result.values))

            # Wait for completion
            self.conn.wait_for_event(
                lambda f: len(f) > 2 and f[2] == '105', timeout=60)
            self.conn.wait_for_event(
                lambda f: len(f) > 3 and f[2] == '101' and f[3].strip() == '0',
                timeout=30)

            # Post-run
            self.conn.query(MSG_STATUS_QUERY)
            self.conn.command(MSG_SET_PARAMETER, b'15')

            self._update_experiment(guid, 'complete')
            log.info("Experiment %s complete", guid)

        except Exception as e:
            log.exception("Experiment %s failed", guid)
            self._update_experiment(guid, 'error', str(e))
        finally:
            self._current_guid = ''

    def _update_experiment(self, guid: str, status: str, error: str = None):
        now = datetime.now(timezone.utc).isoformat() if status in ('complete', 'error') else None
        with self._db_lock:
            self.db.execute(
                'UPDATE experiments SET status=?, completed_at=?, error_message=? WHERE guid=?',
                (status, now, error, guid))
            self.db.commit()

    def _event_logger(self):
        """Background thread that logs events to SQLite."""
        while self.conn and self.conn._running:
            with self.conn._events_lock:
                events = list(self.conn._events)
            for fields in events:
                if len(fields) < 3:
                    continue
                ts = fields[1] if len(fields) > 1 else ''
                code = int(fields[2]) if fields[2].isdigit() else 0
                sub = fields[3].strip() if len(fields) > 3 else ''
                msg = ' '.join(f.strip() for f in fields[4:]) if len(fields) > 4 else ''
                with self._db_lock:
                    self.db.execute(
                        'INSERT INTO events (timestamp, event_code, sub_code, message, raw_fields) '
                        'VALUES (?, ?, ?, ?, ?)',
                        (ts, code, sub, msg, json.dumps(fields)))
                    self.db.commit()
            time.sleep(2.0)


# ---------------------------------------------------------------------------
# Door Controller (SwitchBot Fingerbot)
# ---------------------------------------------------------------------------

class DoorController:
    """Controls a SwitchBot Fingerbot via BLE."""

    def __init__(self, mac: str | None = None):
        self.mac = mac
        self._device = None

    async def _get_device(self):
        if not self.mac:
            raise RuntimeError("No Fingerbot MAC configured (--fingerbot-mac or FINGERBOT_MAC)")
        if self._device is None:
            from switchbot import SwitchbotDevice
            from bleak import BleakScanner
            ble_device = await BleakScanner.find_device_by_address(self.mac, timeout=10)
            if not ble_device:
                raise RuntimeError(f"Fingerbot {self.mac} not found via BLE")
            from switchbot.devices.bot import SwitchbotBot
            self._device = SwitchbotBot(ble_device)
        return self._device

    async def open(self) -> tuple[bool, str]:
        try:
            dev = await self._get_device()
            await dev.turn_on()
            return True, ''
        except Exception as e:
            return False, str(e)

    async def close(self) -> tuple[bool, str]:
        try:
            dev = await self._get_device()
            await dev.turn_off()
            return True, ''
        except Exception as e:
            return False, str(e)

    async def status(self) -> dict:
        try:
            dev = await self._get_device()
            info = await dev.get_basic_info()
            return {
                'available': True,
                'battery_percent': info.get('battery', 0) if info else 0,
            }
        except Exception as e:
            return {'available': False, 'battery_percent': 0, 'error': str(e)}


# ---------------------------------------------------------------------------
# Connect RPC Service
# ---------------------------------------------------------------------------

class LightCyclerServiceImpl:
    """Connect RPC service implementation."""

    def __init__(self, manager: LightCyclerManager, door: DoorController,
                 db: sqlite3.Connection):
        self.manager = manager
        self.door = door
        self.db = db
        self._db_lock = manager._db_lock

    async def get_state(self, request: pb.GetStateRequest,
                        ctx: RequestContext) -> pb.GetStateResponse:
        state = self.manager.get_state()
        return pb.GetStateResponse(
            instrument_state=state.get('instrument_state', 0),
            firmware_version=state.get('firmware_version', ''),
            controller_count=state.get('controller_count', ''),
            connected=state.get('connected', False),
            current_experiment_guid=state.get('current_experiment_guid', ''),
        )

    async def get_logs(self, request: pb.GetLogsRequest,
                       ctx: RequestContext) -> pb.GetLogsResponse:
        limit = request.limit or 100
        since_id = request.since_id or 0
        with self._db_lock:
            rows = self.db.execute(
                'SELECT id, timestamp, event_code, sub_code, message '
                'FROM events WHERE id > ? ORDER BY id DESC LIMIT ?',
                (since_id, limit)).fetchall()
        entries = [pb.LogEntry(id=r['id'], timestamp=r['timestamp'],
                               event_code=r['event_code'],
                               sub_code=r['sub_code'] or '',
                               message=r['message'] or '')
                   for r in rows]
        return pb.GetLogsResponse(entries=entries)

    async def run_experiment(self, request: pb.RunExperimentRequest,
                             ctx: RequestContext) -> pb.RunExperimentResponse:
        guid = self.manager.run_experiment(
            temp_c=request.temp_c or 37.0,
            hold_time_s=request.hold_time_s or 1.0,
            num_acquisitions=request.num_acquisitions or 3,
            filter_set=request.filter or 1,
            well_count=request.well_count or 384,
            volume_ul=request.volume_ul or 20,
        )
        return pb.RunExperimentResponse(guid=guid)

    async def get_experiment(self, request: pb.GetExperimentRequest,
                             ctx: RequestContext) -> pb.GetExperimentResponse:
        with self._db_lock:
            row = self.db.execute(
                'SELECT * FROM experiments WHERE guid = ?',
                (request.guid,)).fetchone()
        if not row:
            from connectrpc.errors import ConnectError
            from connectrpc.code import Code
            raise ConnectError(Code.NOT_FOUND, f"Experiment {request.guid} not found")

        config = json.loads(row['config'])
        status_map = {'pending': pb.PENDING, 'running': pb.RUNNING,
                      'complete': pb.COMPLETE, 'error': pb.ERROR}

        with self._db_lock:
            acq_rows = self.db.execute(
                'SELECT * FROM acquisitions WHERE experiment_guid = ? ORDER BY acquisition_num',
                (request.guid,)).fetchall()

        acquisitions = []
        for ar in acq_rows:
            acquisitions.append(pb.Acquisition(
                acquisition_num=ar['acquisition_num'],
                temperature_c=ar['temperature_c'] or 0,
                time_s=ar['time_s'] or 0,
                ref_channel=ar['ref_channel'] or 0,
                well_values=json.loads(ar['well_data']),
            ))

        return pb.GetExperimentResponse(
            guid=row['guid'],
            status=status_map.get(row['status'], pb.STATUS_UNSPECIFIED),
            config=pb.RunExperimentRequest(
                temp_c=config.get('temp_c', 37.0),
                hold_time_s=config.get('hold_time_s', 1.0),
                num_acquisitions=config.get('num_acquisitions', 3),
                filter=config.get('filter_set', 1),
                well_count=config.get('well_count', 384),
                volume_ul=config.get('volume_ul', 20),
            ),
            created_at=row['created_at'] or '',
            completed_at=row['completed_at'] or '',
            error_message=row['error_message'] or '',
            acquisitions=acquisitions,
        )

    async def list_experiments(self, request: pb.ListExperimentsRequest,
                               ctx: RequestContext) -> pb.ListExperimentsResponse:
        limit = request.limit or 50
        with self._db_lock:
            rows = self.db.execute(
                'SELECT guid, status, created_at, '
                '(SELECT COUNT(*) FROM acquisitions WHERE experiment_guid = experiments.guid) as n_acq '
                'FROM experiments ORDER BY created_at DESC LIMIT ?',
                (limit,)).fetchall()
        status_map = {'pending': pb.PENDING, 'running': pb.RUNNING,
                      'complete': pb.COMPLETE, 'error': pb.ERROR}
        return pb.ListExperimentsResponse(
            experiments=[pb.ListExperimentsResponse.ExperimentSummary(
                guid=r['guid'],
                status=status_map.get(r['status'], pb.STATUS_UNSPECIFIED),
                created_at=r['created_at'] or '',
                num_acquisitions=r['n_acq'],
            ) for r in rows])

    async def open_door(self, request: pb.OpenDoorRequest,
                        ctx: RequestContext) -> pb.OpenDoorResponse:
        ok, err = await self.door.open()
        return pb.OpenDoorResponse(success=ok, error=err)

    async def close_door(self, request: pb.CloseDoorRequest,
                         ctx: RequestContext) -> pb.CloseDoorResponse:
        ok, err = await self.door.close()
        return pb.CloseDoorResponse(success=ok, error=err)

    async def get_door_status(self, request: pb.GetDoorStatusRequest,
                              ctx: RequestContext) -> pb.GetDoorStatusResponse:
        s = await self.door.status()
        return pb.GetDoorStatusResponse(
            battery_percent=s.get('battery_percent', 0),
            available=s.get('available', False),
            error=s.get('error', ''),
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def create_app(lc_host: str, lc_port: int, db_path: str,
               fingerbot_mac: str | None, mock: bool, mock_speed: float):
    db = init_db(db_path)
    manager = LightCyclerManager(
        '127.0.0.1' if mock else lc_host,
        lc_port, db, mock=mock, mock_speed=mock_speed)
    manager.connect()
    door = DoorController(fingerbot_mac)
    service = LightCyclerServiceImpl(manager, door, db)
    return LightCyclerServiceASGIApplication(service)


def main():
    parser = argparse.ArgumentParser(description='LightCycler Connect RPC Server')
    parser.add_argument('--host', default='192.168.95.41', help='LightCycler IP')
    parser.add_argument('--port', type=int, default=5100, help='LightCycler base port')
    parser.add_argument('--listen', default='0.0.0.0:8080', help='Server listen address')
    parser.add_argument('--db', default='lightcycler.db', help='SQLite database path')
    parser.add_argument('--fingerbot-mac', default=os.environ.get('FINGERBOT_MAC'),
                        help='SwitchBot Fingerbot BLE MAC address')
    parser.add_argument('--mock', action='store_true', help='Use mock LightCycler')
    parser.add_argument('--mock-speed', type=float, default=50.0)
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-5s %(message)s', datefmt='%H:%M:%S')

    listen_host, listen_port = args.listen.rsplit(':', 1)
    app = create_app(args.host, args.port, args.db,
                     args.fingerbot_mac, args.mock, args.mock_speed)

    import uvicorn
    uvicorn.run(app, host=listen_host, port=int(listen_port))


if __name__ == '__main__':
    main()
