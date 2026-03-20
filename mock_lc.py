#!/usr/bin/env python3
"""LightCycler 480 II - Mock Simulator Server

Replays realistic LC behavior for offline testing without the instrument.
Run standalone or use from run_experiment.py --mock.
"""
from __future__ import annotations

import argparse
import logging
import queue
import random
import socket
import threading
import time

from lightcycler import (
    LightCyclerPacket,
    MSG_KEEPALIVE, MSG_EVENT, MSG_TRACE,
    MSG_STATUS_QUERY, MSG_START_RUN,
    MSG_CREATE_EXPERIMENT, MSG_FINALIZE_EXPERIMENT,
    MSG_DEFINE_PROGRAM, MSG_FINALIZE_PROGRAM,
    MSG_STEP_PARAMETERS, MSG_ACQUISITION_POINT,
    MSG_GET_RESULT_DATA, MSG_ACK_RESULT, MSG_QUERY_RESULT_INFO,
    MSG_QUERY_LOAD_STATE, MSG_SET_PARAMETER, MSG_NAMES,
)

log = logging.getLogger(__name__)


class MockLightCycler:
    """TCP server that simulates an LC480 instrument."""

    def __init__(self, host='127.0.0.1', port=5100, speed=10.0):
        self.host = host
        self.port = port
        self.speed = speed
        self.server = None

    def start(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.host, self.port))
        self.server.listen(1)
        log.info("Mock LC listening on %s:%d (%.0fx speed)",
                 self.host, self.port, self.speed)

    def serve_one(self):
        """Accept and serve one client connection (blocking)."""
        conn, addr = self.server.accept()
        log.info("Client connected from %s", addr)
        session = _MockSession(conn, self.speed)
        try:
            session.run()
        except Exception:
            log.exception("Session error")
        finally:
            conn.close()
            log.info("Client disconnected")

    def stop(self):
        if self.server:
            self.server.close()


class _MockSession:
    """Handles one client connection through a full experiment lifecycle."""

    def __init__(self, conn, speed):
        self.conn = conn
        self.conn.settimeout(0.1)
        self.speed = speed
        self.running = True
        self._send_lock = threading.Lock()
        self._commands = queue.Queue()
        self._recv_buf = b''
        self._seq = 100
        # Experiment state (learned from commands)
        self.guid = 'MOCK' + '0' * 28
        self.well_count = 384
        self.num_acquisitions = 3

    def run(self):
        threading.Thread(target=self._recv_loop, daemon=True).start()
        self._phase_init()
        self._phase_loading()
        self._phase_commands()
        self._phase_run()
        self._phase_finish()

    # --- Helpers ---

    def _delay(self, secs):
        time.sleep(secs / self.speed)

    def _next_seq(self):
        self._seq += 1
        if self._seq > 106:
            self._seq = 101
        return self._seq

    def _send(self, pkt):
        with self._send_lock:
            try:
                self.conn.sendall(pkt.encode())
            except Exception:
                self.running = False

    def _send_event(self, code, *data_lines):
        """Send a 0x000E event. Payload: seq\\nts\\ncode\\n[data...]\\n\\n"""
        seq = self._next_seq()
        ts = time.strftime("%d.%m.%Y_%H:%M:%S")
        parts = [str(seq), ts, str(code)] + [str(d) for d in data_lines]
        payload = '\n'.join(parts) + '\n\n'
        self._send(LightCyclerPacket(MSG_EVENT, payload.encode('ascii')))

    def _respond_ok(self, msg_type, seq, extra=None):
        """Standard command response: 0\\nseq\\n0[\\nextra...]\\n"""
        fields = ['0', str(seq), '0']
        if extra:
            fields.extend(str(e) for e in extra)
        payload = '\n'.join(fields) + '\n'
        self._send(LightCyclerPacket(msg_type, payload.encode('ascii')))

    def _get_command(self, timeout=60):
        """Block until a non-keepalive, non-ACK command arrives."""
        deadline = time.time() + timeout
        while self.running:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("No command received")
            try:
                return self._commands.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
        raise ConnectionError("Session ended")

    def _expect(self, expected_type, timeout=60):
        """Wait for a specific command type, handling others generically."""
        deadline = time.time() + timeout
        while self.running:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Expected {MSG_NAMES.get(expected_type, hex(expected_type))}"
                )
            try:
                pkt = self._commands.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if pkt.msg_type == expected_type:
                return pkt
            self._handle_generic(pkt)
        raise ConnectionError("Session ended")

    def _handle_generic(self, pkt):
        """Handle a command we weren't specifically expecting."""
        fields = pkt.fields
        seq = fields[0] if fields else '0'
        if pkt.msg_type == MSG_STATUS_QUERY:
            self._send_status()
        elif pkt.msg_type == MSG_QUERY_LOAD_STATE:
            self._send(LightCyclerPacket(MSG_QUERY_LOAD_STATE, b'0\n\n\n'))
        elif pkt.msg_type == MSG_SET_PARAMETER:
            self._send(LightCyclerPacket(MSG_SET_PARAMETER, b'0\n'))
        else:
            self._respond_ok(pkt.msg_type, seq)

    # --- Recv Thread ---

    def _recv_loop(self):
        while self.running:
            try:
                data = self.conn.recv(4096)
                if not data:
                    self.running = False
                    break
                self._recv_buf += data
            except socket.timeout:
                continue
            except Exception:
                self.running = False
                break
            while True:
                pkt, n = LightCyclerPacket.decode(self._recv_buf)
                if not pkt:
                    break
                self._recv_buf = self._recv_buf[n:]
                if pkt.msg_type == MSG_KEEPALIVE:
                    self._send(LightCyclerPacket(MSG_KEEPALIVE, b'Connection'))
                elif pkt.msg_type in (MSG_EVENT, MSG_TRACE):
                    pass  # ACK from HOST, ignore
                else:
                    self._commands.put(pkt)

    # --- Simulation Phases ---

    def _phase_init(self):
        """Simulate the ~61s initialization sequence."""
        self._delay(1.0)
        steps = [
            (84, 'FilterWheels initialisation done.'),
            (85, 'WellCoordinates calculation started.'),
            (86, 'WellCoordinates calculation done.'),
            (87, 'HotPixel search started.'),
            (88, 'HotPixel search done.'),
            (89, 'DarkValue measurement started.'),
            (90, 'Detection done.'),
            (95, 'RunSequencer, Detection  done.'),
            (100, 'Initialisation of Instrument finished.'),
        ]
        for progress, msg in steps:
            self._send_event(
                90, '11', f' {progress}', ' 0',
                f' Initialisation of Instrument. {msg}',
            )
            self._delay(0.3)

        # State transitions: cleanup -> recovery
        self._send_event(11, 5)
        self._send_event(11, 6)

    def _phase_loading(self):
        """Simulate plate detection and loading."""
        self._delay(0.5)
        self._send_event(301, 5)    # Cleanup done
        self._delay(0.5)
        self._send_event(303, '1 ')  # Plate detected
        self._delay(0.3)
        self._send_event(301, 4)    # Loaded
        self._send_event(11, 10)    # Idle

    def _phase_commands(self):
        """Handle pre-run queries and experiment definition until StartRun."""
        while self.running:
            pkt = self._get_command()
            fields = pkt.fields
            seq = fields[0] if fields else '0'

            if pkt.msg_type == MSG_QUERY_LOAD_STATE:
                self._send(LightCyclerPacket(
                    MSG_QUERY_LOAD_STATE, b'0\n\n\n'))

            elif pkt.msg_type == MSG_SET_PARAMETER:
                self._send(LightCyclerPacket(MSG_SET_PARAMETER, b'0\n'))

            elif pkt.msg_type == MSG_STATUS_QUERY:
                self._send_status()

            elif pkt.msg_type == MSG_CREATE_EXPERIMENT:
                if len(fields) > 2:
                    self.guid = fields[1]
                    self.well_count = int(fields[2])
                self._respond_ok(pkt.msg_type, seq)

            elif pkt.msg_type == MSG_DEFINE_PROGRAM:
                if len(fields) > 6:
                    self.num_acquisitions = int(fields[6])
                self._respond_ok(pkt.msg_type, seq)

            elif pkt.msg_type == MSG_START_RUN:
                self._respond_ok(pkt.msg_type, seq, extra=[1])
                return  # Proceed to run phase

            else:
                # StepParameters, AcquisitionPoint, Finalize*, etc.
                self._respond_ok(pkt.msg_type, seq)

    def _phase_run(self):
        """Simulate the experiment run: warm-up, acquisitions, data."""
        # State -> running
        self._send_event(11, 7)

        # Warm-up
        self._send_event(
            90, '12', ' 0', ' 0',
            ' Instrument Warm Up. This may take several minutes.\nPlease wait.',
        )
        self._delay(1.0)
        self._send_event(301, 6)
        self._send_event(
            90, '12', ' 100', ' 0',
            ' Instrument Warm Up finished.',
        )

        # Run started
        self._send_event(104)

        # Thermal ramp data
        self._send_event(
            202, '5 280 1969 690 2016 1100 2178 1520 2408 1930 2662')
        self._delay(0.5)
        self._send_event(
            202, '5 2340 2901 2750 3170 3170 3361 3580 3554 3990 3751')

        # Acquisitions
        for acq in range(1, self.num_acquisitions + 1):
            self._delay(0.5)

            # Thermal data during acquisition
            t_base = 4400 + (acq - 1) * 2100
            self._send_event(
                202,
                f'5 {t_base} 3700 {t_base+410} 3700 '
                f'{t_base+820} 3700 {t_base+1230} 3700 {t_base+1640} 3700',
            )

            # Acquisition complete
            self._send_event(103, f'1 1 {acq}')

            # Last acquisition: cycle + run complete events
            if acq == self.num_acquisitions:
                self._send_event(102, f'1 1 {acq}')
                self._send_event(105)

            self._delay(0.2)

            # Data ready
            self._send_event(201, f'{self.well_count} 1 1 {acq}')

            # HOST fetches data
            self._expect(MSG_GET_RESULT_DATA)
            self._send_result_data(acq)

            self._expect(MSG_QUERY_RESULT_INFO)
            self._send_result_info(acq)

            self._expect(MSG_ACK_RESULT)
            self._send(LightCyclerPacket(MSG_ACK_RESULT, b'0\n'))

    def _phase_finish(self):
        """Post-run: success status, idle, handle cleanup queries."""
        self._delay(0.5)
        self._send_event(101, '0')   # Run success
        self._send_event(11, 10)     # Back to idle

        # Handle post-run queries until client stops sending
        for _ in range(10):
            try:
                pkt = self._get_command(timeout=5)
            except (TimeoutError, ConnectionError):
                break
            self._handle_generic(pkt)

    # --- Response Builders ---

    def _send_status(self):
        """Send a realistic 0x0033 status response (25 fields)."""
        fields = [
            '248671', '1495261', '293098', '67', '58',
            '4064', '4057', '1', '20', '0',
            '45', '16946', '8250', '2277', '51',
            '555043', '6', '6674', '6477', '2500',
            '0', '1625644356', '1065064231', '1129670778', '1200000',
        ]
        payload = '\n'.join(fields) + '\n'
        self._send(LightCyclerPacket(
            MSG_STATUS_QUERY, payload.encode('ascii')))

    def _send_result_data(self, acq_num):
        """Generate a mock 0x044D result with random fluorescence values."""
        values = [random.randint(25, 70) for _ in range(self.well_count)]
        # Scatter some brighter wells to look realistic
        for i in range(0, self.well_count, 48):
            values[i] = random.randint(80, 200)

        time_cs = 10000 + acq_num * 3400
        temp_cd = 3700 + random.randint(-10, 10)

        fields = [
            '0', '1', '2', '1', self.guid,
            '1', '1', str(acq_num),
            str(time_cs), str(temp_cd),
            '389949', '12765', '0', str(self.well_count),
        ] + [str(v) for v in values]

        payload = '\n'.join(fields) + '\n'
        self._send(LightCyclerPacket(
            MSG_GET_RESULT_DATA, payload.encode('ascii')))

    def _send_result_info(self, acq_num):
        """0x044F response: status, block, acq, wells."""
        payload = f'0\n1\n{acq_num}\n{self.well_count}\n'
        self._send(LightCyclerPacket(
            MSG_QUERY_RESULT_INFO, payload.encode('ascii')))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Mock LightCycler 480 Simulator')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5100)
    parser.add_argument('--speed', type=float, default=10.0,
                        help='Speed multiplier (default: 10x real-time)')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-5s %(name)s: %(message)s',
    )

    mock = MockLightCycler(args.host, args.port, args.speed)
    mock.start()
    try:
        while True:
            mock.serve_one()
    except KeyboardInterrupt:
        pass
    finally:
        mock.stop()
