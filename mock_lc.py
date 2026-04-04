#!/usr/bin/env python3
"""LightCycler 480 II - Mock Simulator Server (Multi-Port)

Simulates the LC480's 5-port TCP architecture for offline testing.

  Port 5100 - Keepalive + handshake
  Port 5101 - Event/trace stream
  Port 5102 - Experiment commands
  Port 5104 - Result data
  Port 5105 - Status polling

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
    MSG_KEEPALIVE, MSG_VERSION_QUERY, MSG_STATUS_4,
    MSG_SYS_INFO, MSG_CTRL_INFO,
    MSG_EVENT, MSG_TRACE,
    MSG_STATUS_QUERY, MSG_START_RUN,
    MSG_CREATE_EXPERIMENT, MSG_FINALIZE_EXPERIMENT,
    MSG_DEFINE_PROGRAM, MSG_FINALIZE_PROGRAM,
    MSG_THERMAL_PARAMS, MSG_PROTOCOL_STEP,
    MSG_GET_RESULT_DATA, MSG_ACK_RESULT, MSG_QUERY_RESULT_INFO,
    MSG_QUERY_LOAD_STATE, MSG_SET_PARAMETER,
    MSG_SUBSYS_STATUS, MSG_SUBSYS_QUERY, MSG_CALIBRATION,
    MSG_NAMES,
    PORT_KEEPALIVE, PORT_EVENTS, PORT_COMMANDS, PORT_RESULTS, PORT_STATUS,
    ALL_PORTS,
)

log = logging.getLogger(__name__)


class MockLightCycler:
    """Multi-port TCP server that simulates an LC480 instrument."""

    def __init__(self, host='127.0.0.1', port=5100, speed=10.0):
        self.host = host
        self.base_port = port
        self.speed = speed
        self._servers: dict[int, socket.socket] = {}

    def start(self):
        offsets = {
            PORT_KEEPALIVE: 0,
            PORT_EVENTS: 1,
            PORT_COMMANDS: 2,
            PORT_RESULTS: 4,
            PORT_STATUS: 5,
        }
        for canonical_port, offset in offsets.items():
            actual_port = self.base_port + offset
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, actual_port))
            srv.listen(1)
            self._servers[canonical_port] = srv
        log.info("Mock LC listening on %s:%d--%d (%.0fx speed)",
                 self.host, self.base_port, self.base_port + 5, self.speed)

    def serve_one(self):
        """Accept connections on all ports and serve one session (blocking)."""
        conns: dict[int, socket.socket] = {}
        try:
            # Accept port 5100 first (client connects here first)
            conn, addr = self._servers[PORT_KEEPALIVE].accept()
            log.info("Client connected on keepalive port from %s", addr)
            conns[PORT_KEEPALIVE] = conn

            # Start handling keepalive + handshake immediately in background
            session = _MockSession(conns, self.speed)
            threading.Thread(target=session.handle_keepalive_port,
                             daemon=True).start()

            # Accept remaining ports (client opens them after handshake)
            for port in [PORT_EVENTS, PORT_COMMANDS, PORT_RESULTS, PORT_STATUS]:
                srv = self._servers[port]
                srv.settimeout(10.0)
                try:
                    c, a = srv.accept()
                    log.info("Client connected on port %d from %s", port, a)
                    conns[port] = c
                except socket.timeout:
                    log.warning("Timeout waiting for connection on port %d", port)
                    conns[port] = None

            session.conns = conns
            session.run()
        except Exception:
            log.exception("Session error")
        finally:
            for c in conns.values():
                if c:
                    try:
                        c.close()
                    except OSError:
                        pass
            log.info("Client disconnected")

    def stop(self):
        for srv in self._servers.values():
            try:
                srv.close()
            except OSError:
                pass
        self._servers.clear()


class _MockSession:
    """Handles one client connection through a full experiment lifecycle."""

    def __init__(self, conns: dict[int, socket.socket | None], speed: float):
        self.conns = conns
        self.speed = speed
        self.running = True
        self._send_locks: dict[int, threading.Lock] = {
            p: threading.Lock() for p in ALL_PORTS
        }
        self._commands: queue.Queue[LightCyclerPacket] = queue.Queue()
        self._recv_bufs: dict[int, bytes] = {p: b'' for p in ALL_PORTS}
        self._seq = 100
        # Experiment state
        self.guid = 'MOCK' + '0' * 28
        self.well_count = 384
        self.num_acquisitions = 3

    def handle_keepalive_port(self):
        """Handle keepalive + handshake on port 5100 (runs in background)."""
        conn = self.conns[PORT_KEEPALIVE]
        conn.settimeout(0.1)
        while self.running:
            try:
                data = conn.recv(4096)
                if not data:
                    self.running = False
                    break
                self._recv_bufs[PORT_KEEPALIVE] += data
            except socket.timeout:
                continue
            except Exception:
                self.running = False
                break
            self._process_keepalive_buf()

    def _process_keepalive_buf(self):
        while True:
            pkt, n = LightCyclerPacket.decode(self._recv_bufs[PORT_KEEPALIVE])
            if not pkt:
                break
            self._recv_bufs[PORT_KEEPALIVE] = self._recv_bufs[PORT_KEEPALIVE][n:]

            if pkt.msg_type == MSG_KEEPALIVE:
                self._send_on(PORT_KEEPALIVE,
                              LightCyclerPacket(MSG_KEEPALIVE, b'Connection'))
            elif pkt.msg_type == MSG_VERSION_QUERY:
                payload = b'8/HTC\n1.8.6.1501\n' + time.strftime(
                    "%d.%m.%Y_%H:%M:%S").encode('ascii')
                self._send_on(PORT_KEEPALIVE,
                              LightCyclerPacket(MSG_VERSION_QUERY, payload))
            elif pkt.msg_type == MSG_EVENT:
                payload = b'3\n1\n1\n1\n1\n1\n'
                self._send_on(PORT_KEEPALIVE,
                              LightCyclerPacket(MSG_EVENT, payload))
            elif pkt.msg_type == MSG_STATUS_4:
                self._send_on(PORT_KEEPALIVE,
                              LightCyclerPacket(MSG_STATUS_4, b'1'))
            elif pkt.msg_type == MSG_SYS_INFO:
                self._send_hw_info()
            elif pkt.msg_type == MSG_CTRL_INFO:
                self._send_ctrl_info()

    def run(self):
        """Main session lifecycle after all ports are connected."""
        # Start recv threads for command/result/status ports
        for port in [PORT_EVENTS, PORT_COMMANDS, PORT_RESULTS, PORT_STATUS]:
            if self.conns.get(port):
                threading.Thread(target=self._recv_loop, args=(port,),
                                 daemon=True).start()

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

    def _send_on(self, port: int, pkt: LightCyclerPacket):
        conn = self.conns.get(port)
        if not conn:
            return
        with self._send_locks[port]:
            try:
                conn.sendall(pkt.encode())
            except Exception:
                self.running = False

    def _send_event(self, code, *data_lines):
        """Send a 0x000E event on port 5101."""
        seq = self._next_seq()
        ts = time.strftime("%d.%m.%Y_%H:%M:%S")
        parts = [str(seq), ts, str(code)] + [str(d) for d in data_lines]
        payload = '\n'.join(parts) + '\n\n'
        self._send_on(PORT_EVENTS,
                      LightCyclerPacket(MSG_EVENT, payload.encode('ascii')))

    def _respond_ok(self, port: int, msg_type: int, seq, extra=None):
        fields = ['0', str(seq), '0']
        if extra:
            fields.extend(str(e) for e in extra)
        payload = '\n'.join(fields) + '\n'
        self._send_on(port,
                      LightCyclerPacket(msg_type, payload.encode('ascii')))

    def _get_command(self, timeout=60):
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
        fields = pkt.fields
        seq = fields[0] if fields else '0'
        if pkt.msg_type == MSG_STATUS_QUERY:
            self._send_status()
        elif pkt.msg_type == MSG_QUERY_LOAD_STATE:
            self._send_on(PORT_RESULTS,
                          LightCyclerPacket(MSG_QUERY_LOAD_STATE, b'0\n\n\n'))
        elif pkt.msg_type == MSG_SET_PARAMETER:
            self._send_on(PORT_RESULTS,
                          LightCyclerPacket(MSG_SET_PARAMETER, b'0\n'))
        elif pkt.msg_type == MSG_SUBSYS_STATUS:
            self._send_on(PORT_STATUS,
                          LightCyclerPacket(MSG_SUBSYS_STATUS, b'3\n'))
        elif pkt.msg_type == MSG_SUBSYS_QUERY:
            payload = b'3\n3\n3\n3\n3\n1\n3\n3\n5\n3\n7589\n9467\n4\n1\n'
            self._send_on(PORT_STATUS,
                          LightCyclerPacket(MSG_SUBSYS_QUERY, payload))
        elif pkt.msg_type == MSG_CALIBRATION:
            # Send minimal calibration stub
            self._send_on(PORT_STATUS,
                          LightCyclerPacket(MSG_CALIBRATION, b'0\n'))
        else:
            # Route response to the correct port based on message type
            port = self._port_for_response(pkt.msg_type)
            self._respond_ok(port, pkt.msg_type, seq)

    def _port_for_response(self, msg_type: int) -> int:
        """Determine which port to send a response on."""
        from lightcycler import _MSG_PORT
        return _MSG_PORT.get(msg_type, PORT_COMMANDS)

    # --- Recv Thread ---

    def _recv_loop(self, port: int):
        conn = self.conns.get(port)
        if not conn:
            return
        conn.settimeout(0.1)
        while self.running:
            try:
                data = conn.recv(4096)
                if not data:
                    break
                self._recv_bufs[port] += data
            except socket.timeout:
                continue
            except Exception:
                break
            while True:
                pkt, n = LightCyclerPacket.decode(self._recv_bufs[port])
                if not pkt:
                    break
                self._recv_bufs[port] = self._recv_bufs[port][n:]
                if pkt.msg_type == MSG_KEEPALIVE:
                    self._send_on(port,
                                  LightCyclerPacket(MSG_KEEPALIVE, b'Connection'))
                elif pkt.msg_type in (MSG_EVENT, MSG_TRACE,
                                      MSG_SYS_INFO, MSG_CTRL_INFO):
                    pass  # ACK from HOST, ignore
                elif pkt.msg_type == MSG_VERSION_QUERY and port == PORT_EVENTS:
                    pass  # Registration message, ignore
                else:
                    self._commands.put(pkt)

    # --- Simulation Phases ---

    def _phase_init(self):
        """Simulate initialization on port 5101."""
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

        self._send_event(11, 5)
        self._send_event(11, 6)

    def _phase_loading(self):
        """Simulate plate detection on port 5101."""
        self._delay(0.5)
        self._send_event(301, 5)
        self._delay(0.5)
        self._send_event(303, '1 ')
        self._delay(0.3)
        self._send_event(301, 4)
        self._send_event(11, 10)

    def _phase_commands(self):
        """Handle pre-run queries and experiment definition until StartRun."""
        while self.running:
            pkt = self._get_command()
            fields = pkt.fields
            seq = fields[0] if fields else '0'

            if pkt.msg_type == MSG_QUERY_LOAD_STATE:
                self._send_on(PORT_RESULTS,
                              LightCyclerPacket(MSG_QUERY_LOAD_STATE, b'0\n\n\n'))
            elif pkt.msg_type == MSG_SET_PARAMETER:
                self._send_on(PORT_RESULTS,
                              LightCyclerPacket(MSG_SET_PARAMETER, b'0\n'))
            elif pkt.msg_type == MSG_STATUS_QUERY:
                self._send_status()
            elif pkt.msg_type == MSG_CREATE_EXPERIMENT:
                if len(fields) > 2:
                    self.guid = fields[1]
                    self.well_count = int(fields[2])
                self._respond_ok(PORT_COMMANDS, pkt.msg_type, seq)
            elif pkt.msg_type == MSG_DEFINE_PROGRAM:
                if len(fields) > 6:
                    self.num_acquisitions = int(fields[6])
                self._respond_ok(PORT_COMMANDS, pkt.msg_type, seq)
            elif pkt.msg_type == MSG_START_RUN:
                self._respond_ok(PORT_COMMANDS, pkt.msg_type, seq, extra=[1])
                return
            elif pkt.msg_type in (MSG_SUBSYS_STATUS, MSG_SUBSYS_QUERY,
                                  MSG_CALIBRATION):
                self._handle_generic(pkt)
            else:
                self._respond_ok(PORT_COMMANDS, pkt.msg_type, seq)

    def _phase_run(self):
        """Simulate experiment: warm-up, acquisitions, data."""
        self._send_event(11, 7)

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

        self._send_event(104)

        self._send_event(
            202, '5 280 1969 690 2016 1100 2178 1520 2408 1930 2662')
        self._delay(0.5)
        self._send_event(
            202, '5 2340 2901 2750 3170 3170 3361 3580 3554 3990 3751')

        for acq in range(1, self.num_acquisitions + 1):
            self._delay(0.5)

            t_base = 4400 + (acq - 1) * 2100
            self._send_event(
                202,
                f'5 {t_base} 3700 {t_base+410} 3700 '
                f'{t_base+820} 3700 {t_base+1230} 3700 {t_base+1640} 3700',
            )

            self._send_event(103, f'1 1 {acq}')

            if acq == self.num_acquisitions:
                self._send_event(102, f'1 1 {acq}')
                self._send_event(105)

            self._delay(0.2)
            self._send_event(201, f'{self.well_count} 1 1 {acq}')

            self._expect(MSG_GET_RESULT_DATA)
            self._send_result_data(acq)

            self._expect(MSG_QUERY_RESULT_INFO)
            self._send_result_info(acq)

            self._expect(MSG_ACK_RESULT)
            self._send_on(PORT_RESULTS,
                          LightCyclerPacket(MSG_ACK_RESULT, b'0\n'))

    def _phase_finish(self):
        """Post-run cleanup."""
        self._delay(0.5)
        self._send_event(101, '0')
        self._send_event(11, 10)

        for _ in range(10):
            try:
                pkt = self._get_command(timeout=5)
            except (TimeoutError, ConnectionError):
                break
            self._handle_generic(pkt)

    # --- Response Builders ---

    def _send_hw_info(self):
        """Send 0x000C hardware info on port 5100."""
        fields = [
            '12345678', '1', '0', '1200', '00DEADBEEF42',
            '168034304', '917504', '131072', '45356',
            '134217728', '134217728', '131072', '45356',
            '2147483648', '2097152', '0', '0', '0',
            '134217728', '0', '0', '0', '0', '0', '0',
            '11111111', '8',
        ]
        payload = '\n'.join(fields) + '\n'
        self._send_on(PORT_KEEPALIVE,
                      LightCyclerPacket(MSG_SYS_INFO, payload.encode('ascii')))

    def _send_ctrl_info(self):
        """Send 0x000D controller info on port 5100."""
        payload = (
            '1 1 3\n4\n'
            'BlockThermoCycler Ctrl\n0\n1\n0\n0\n'
            'HTCCntrl\n1.02.00\n1526   \n22775\n'
            'HTCCntrl\n1.02.00\n1526   \n22775\n'
            'CYG_BOOT\n1.00.00\n0517   \n00076579\n'
            '04513754\n L\n04513754001\n'
            'DC Motors Ctrl\n0\n2\n0\n0\n'
            'DC-SLAVE\n1.00.03\n0711   \n63108\n'
            'DC-SLAVE\n1.00.03\n0711   \n63108\n'
            'CYG_BOOT\n1.00.00\n0517   \n00076579\n'
            '04513754\n L\n04513754001\n'
            'DetectionControl Ctrl\n0\n4\n0\n0\n'
            'DETCTRL \n1.02.00\n1137   \n740\n'
            'DETCTRL \n1.02.00\n1137   \n740\n'
            'CYG_BOOT\n1.00.00\n0517   \n00071773\n'
            '04479785\nH \n04479785001\n'
            'RawdataReduction Ctrl\n0\n3\n0\n0\n'
            'DrDspApp\n1.04.02\n1322   \n-779133962\n'
            'DrDspApp\n1.04.02\n1322   \n-779133962\n'
            'AppLoade\n2.00.00\n0508   \n00071651\n'
            '04479807\nF \n04479807001\n'
        )
        self._send_on(PORT_KEEPALIVE,
                      LightCyclerPacket(MSG_CTRL_INFO, payload.encode('ascii')))

    def _send_status(self):
        """Send 0x0033 status on port 5105."""
        fields = [
            '248671', '1495261', '293098', '67', '58',
            '4064', '4057', '1', '20', '0',
            '45', '16946', '8250', '2277', '51',
            '555043', '6', '6674', '6477', '2500',
            '0', '1625644356', '1065064231', '1129670778', '1200000',
        ]
        payload = '\n'.join(fields) + '\n'
        self._send_on(PORT_STATUS,
                      LightCyclerPacket(MSG_STATUS_QUERY, payload.encode('ascii')))

    def _send_result_data(self, acq_num):
        values = [random.randint(25, 70) for _ in range(self.well_count)]
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
        self._send_on(PORT_RESULTS,
                      LightCyclerPacket(MSG_GET_RESULT_DATA,
                                        payload.encode('ascii')))

    def _send_result_info(self, acq_num):
        payload = f'0\n1\n{acq_num}\n{self.well_count}\n'
        self._send_on(PORT_RESULTS,
                      LightCyclerPacket(MSG_QUERY_RESULT_INFO,
                                        payload.encode('ascii')))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Mock LightCycler 480 Simulator (Multi-Port)')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5100,
                        help='Base port (listens on port, port+1, +2, +4, +5)')
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
