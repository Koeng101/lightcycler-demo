#!/usr/bin/env python3
"""LightCycler 480 II - TCP Protocol Implementation

Reverse-engineered protocol for the Roche LightCycler 480 II qPCR instrument.

================================================================================
WIRE FORMAT
================================================================================

Every message (both directions) uses the same 4-byte header + ASCII payload:

    ┌──────────────────┬────────────────────┬─────────────────────────┐
    │ msg_type (2B BE) │ length (2B BE)     │ payload (ASCII, \\n-delim)│
    └──────────────────┴────────────────────┴─────────────────────────┘

Example - keepalive "hi":  00 00 00 02 68 69
                           ╰type╯ ╰len╯ ╰"hi"╯

Payloads are newline-delimited (\\n) ASCII text fields. No binary data in
payloads. Numeric values are sent as decimal ASCII strings.

================================================================================
5-PORT ARCHITECTURE
================================================================================

The firmware listens on 5 TCP ports. Each port handles a specific function:

    Port   Role                  Message types (hex)
    ────   ────                  ───────────────────
    5100   Keepalive + handshake 0x0000 keepalive
                                 0x0001, 0x000E, 0x0004, 0x000C, 0x000D (init)
    5101   Event / trace stream  0x000E events, 0x0010 traces,
                                 0x000C/0x000D/0x0013 logs (all auto-ACKed)
    5102   Experiment commands   0x012D, 0x012F, 0x0131, 0x0132,
                                 0x0130, 0x012E, 0x00C9
    5104   Result data           0x044D, 0x044E, 0x044F, 0x047F, 0x04B0
    5105   Status polling        0x002F, 0x0030, 0x0033, 0x0046

Port 5100 carries ONLY keepalives and the one-time startup handshake.
Events, commands, results, and status each have their own dedicated port.

================================================================================
CONNECTION STARTUP
================================================================================

1. TCP connect to port 5100
2. Send keepalive: 0x0000 "hi" → LC responds "Connection"
3. Handshake queries on port 5100 (all use "?" as payload):
     0x0001 "?" → firmware version   "8/HTC\\n1.8.6.1501\\n<timestamp>"
     0x000E "?" → event state        "3\\n1\\n1\\n1\\n1\\n1\\n"
     0x0004 "?" → status flag        "1"
     0x000C "?" → hardware info      27 fields (serial, MAC, capabilities)
     0x000D "?" → controller info    4 subsystems with firmware versions
4. TCP connect to ports 5101, 5102, 5104, 5105
5. Register on port 5101:
     Send 0x0001 with payload "DD.MM.YY_HH.MM.SS_4" (timestamp)
     LC replays buffered historical events, then sends live events.
6. Continue keepalives every 1s on port 5100

================================================================================
COMMAND / RESPONSE PATTERN
================================================================================

Commands (ports 5102, 5104, 5105) use sequence numbers for matching:

    HOST sends:                     LC responds (same msg_type):
    ┌─────────────┐                ┌─────────────┐
    │ <seq>       │                │ 0            │  ← status (0 = OK)
    │ <field_1>   │    ────►       │ <seq>        │  ← echoed sequence
    │ <field_2>   │                │ 0            │  ← error code (0 = none)
    │ ...         │                │ [extra...]   │  ← optional extra fields
    └─────────────┘                └─────────────┘

Sequence numbers auto-increment starting from 1 for each experiment.

================================================================================
EXPERIMENT DEFINITION (port 5102)
================================================================================

An experiment is a sequence of protocol steps, each defining a target
temperature. The machine executes steps sequentially: ramp to temp, hold,
read fluorescence, then move to the next step.

    CreateExperiment (0x012D)
    └── Program (0x012F)
        ├── ThermalParams (0x0131)    ← shared hold/ramp for all steps
        ├── ProtocolStep (0x0132)     ← step 1: temp + filter + exposure
        ├── ProtocolStep (0x0132)     ← step 2: can be different temp
        ├── ProtocolStep (0x0132)     ← step 3: ...
        ├── FinalizeProgram (0x0130)
        ├── FinalizeExperiment (0x012E)
        └── StartRun (0x00C9)

Each ProtocolStep (0x0132) IS a step in the protocol. It defines:
  - The target temperature (the machine ramps to it)
  - Filter and exposure settings for the fluorescence read
  - Its own ramp rate

ThermalParams (0x0131) sets SHARED parameters for all steps:
  - Hold time at each temperature
  - Detection mode (2 = read fluorescence)
  - Ramp time

Multiple programs per experiment ARE supported — set num_programs in
CreateExperiment. Set filter=0 on a ProtocolStep for a thermal-only hold (no fluorescence
read). Set filter=1-4 to read with a specific filter set. This allows PCR
protocols with reads only at the annealing step.

Field details (fields marked [?] have unknown meaning, keep default):

    CreateExperiment (0x012D):
      seq, GUID (32-char hex), well_count (384|96), volume_uL (20), num_programs

    DefineProgram (0x012F):
      seq, program_num (1..N), [?]=1, [?]=0, [?]=1, [?]=1, num_steps

    ThermalParams (0x0131):
      seq, program_num, [?]=1, [?]=0, ramp_time_ms (3000),
      [?]=1, [?]=2, [?]=1, hold_time_centisecs

    ProtocolStep (0x0132):
      seq, program_num, step_num (1..N), target_temp_centideg,
      1, exposure (4800), ramp_rate_centideg_per_s, 0, 0, 0, filter_set

    FinalizeProgram (0x0130):  seq, "?"
    FinalizeExperiment (0x012E):  seq, "?"
    StartRun (0x00C9):  seq, "?"

Example 1: 3 fluorescence reads at 37°C, 1s hold:

    CreateExperiment:   1, <GUID>, 384, 20, 1
    DefineProgram:      2, 1, 1, 0, 1, 1, 3       ← 1 cycle, 3 steps
    ThermalParams:      3, 1, 1, 0, 3000, 1, 2, 1, 100
    ProtocolStep:       4, 1, 1, 3700, 1, 4800, 500, 0, 0, 0, 1   ← 37°C
    ProtocolStep:       5, 1, 2, 3700, 1, 4800, 500, 0, 0, 0, 1   ← 37°C
    ProtocolStep:       6, 1, 3, 3700, 1, 4800, 500, 0, 0, 0, 1   ← 37°C

Example 2: read at 37°C then ramp to 50°C and read:

    DefineProgram:      2, 1, 1, 0, 1, 1, 2       ← 1 cycle, 2 steps
    ThermalParams:      3, 1, 1, 0, 3000, 1, 2, 1, 100
    ProtocolStep:       4, 1, 1, 3700, ...         ← 37°C
    ProtocolStep:       5, 1, 2, 5000, ...         ← 50°C
    (confirmed: Acq1=36.96°C, Acq2=51.37°C)

Units:
  - Temperature: centidegrees (3700 = 37.00°C)
  - Hold time: centiseconds (100 = 1.00s)
  - Ramp time: milliseconds (3000 = 3.0s)
  - Filter set: 1=SYBR Green/FAM, 2=HEX/VIC, 3=ROX, 4=Cy5

================================================================================
EVENT STREAM (port 5101)
================================================================================

The LC sends unsolicited events; the host ACKs by echoing the sequence number.

    LC sends:                           HOST ACKs:
    ┌─────────────────────────────┐    ┌──────┐
    │ <seq>                       │    │ <seq>│
    │ <timestamp DD.MM.YYYY_...>  │    └──────┘
    │ <event_code>                │
    │ <sub_fields...>             │
    └─────────────────────────────┘

Key event codes:
    90   Init/warmup progress    sub=11 progress 0-100 (init)
                                 sub=12 progress 0-100 (warm-up)
    11   State change            value: 6=post-init, 7=running, 10=idle
    104  Run started
    105  Run complete (temperature)
    201  Acquisition complete    <wells> <program> <step> <cycle>
    202  Thermal profile data    <count> <time1> <temp1> ...
    301  Phase transition        3=detection, 4=loaded, 5=cleanup, 6=ready
    303  Plate detected          "1 "
    101  Run result              0=success
    103  Cycle complete          <program> <step> <cycle>

Machine must reach state 10 (idle) before experiment commands are accepted.
Plate must be physically loaded (event 303) before reaching state 10.

================================================================================
RESULT DATA (port 5104)
================================================================================

After each acquisition, the host fetches fluorescence data:

    1. Wait for event 201 (acquisition complete)
    2. Send 0x044D with acquisition number → get well data
    3. Send 0x044F " " → get result metadata
    4. Send 0x044E with acquisition number → acknowledge/clear

    0x044D response fields:
      [0]  status (0=OK)         [7]  acquisition number
      [1]  format_version (1)    [8]  elapsed_centisecs
      [2]  data_type (2)         [9]  temperature_centideg
      [3]  block_id (1)          [10] exposure_config
      [4]  experiment_GUID       [11] ref_channel_value
      [5]  program (1)           [12] reserved (0)
      [6]  step (1)              [13] well_count (384)
                                 [14..] fluorescence values (one per well)
"""
from __future__ import annotations

import struct
import socket
import threading
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# --- Message Types ---
MSG_KEEPALIVE           = 0x0000
MSG_VERSION_QUERY       = 0x0001
MSG_STATUS_4            = 0x0004
MSG_SYS_INFO            = 0x000C
MSG_CTRL_INFO           = 0x000D
MSG_EVENT               = 0x000E
MSG_TRACE               = 0x0010
MSG_STATUS_QUERY        = 0x0033
MSG_START_RUN           = 0x00C9
MSG_CREATE_EXPERIMENT   = 0x012D
MSG_FINALIZE_EXPERIMENT = 0x012E
MSG_DEFINE_PROGRAM      = 0x012F
MSG_FINALIZE_PROGRAM    = 0x0130
MSG_THERMAL_PARAMS     = 0x0131
MSG_PROTOCOL_STEP   = 0x0132
MSG_LOG_ENTRY_2         = 0x0012
MSG_LOG_ENTRY           = 0x0013
MSG_SUBSYS_STATUS       = 0x002F
MSG_SUBSYS_QUERY        = 0x0030
MSG_CALIBRATION         = 0x0046
MSG_GET_RESULT_DATA     = 0x044D
MSG_ACK_RESULT          = 0x044E
MSG_QUERY_RESULT_INFO   = 0x044F
MSG_QUERY_LOAD_STATE    = 0x047F
MSG_SET_PARAMETER       = 0x04B0

MSG_NAMES = {
    0x0000: "Keepalive",       0x0001: "VersionQuery",
    0x0004: "Status4",         0x000C: "SysInfo",
    0x000D: "CtrlInfo",        0x000E: "Event",
    0x0010: "Trace",           0x0013: "LogEntry",
    0x002F: "SubsysStatus",
    0x0030: "SubsysQuery",     0x0033: "StatusQuery",
    0x0046: "Calibration",     0x00C9: "StartRun",
    0x012D: "CreateExperiment",0x012E: "FinalizeExpt",
    0x012F: "DefineProgram",   0x0130: "FinalizeProgram",
    0x0131: "ThermalParams",  0x0132: "ProtocolStep",
    0x044D: "GetResultData",   0x044E: "AckResult",
    0x044F: "QueryResultInfo", 0x047F: "QueryLoadState",
    0x04B0: "SetParameter",
}

# Port assignment: which port each message type uses for sending
PORT_KEEPALIVE = 5100
PORT_EVENTS    = 5101
PORT_COMMANDS  = 5102
PORT_RESULTS   = 5104
PORT_STATUS    = 5105

ALL_PORTS = [PORT_KEEPALIVE, PORT_EVENTS, PORT_COMMANDS, PORT_RESULTS, PORT_STATUS]

# Mapping: msg_type -> port for outgoing commands/queries
_MSG_PORT = {
    MSG_KEEPALIVE:           PORT_KEEPALIVE,
    MSG_VERSION_QUERY:       PORT_KEEPALIVE,  # handshake only
    MSG_STATUS_4:            PORT_KEEPALIVE,  # handshake only
    # Events/traces go on 5101 (for ACKs)
    MSG_EVENT:               PORT_EVENTS,
    MSG_TRACE:               PORT_EVENTS,
    # Experiment commands
    MSG_START_RUN:           PORT_COMMANDS,
    MSG_CREATE_EXPERIMENT:   PORT_COMMANDS,
    MSG_FINALIZE_EXPERIMENT: PORT_COMMANDS,
    MSG_DEFINE_PROGRAM:      PORT_COMMANDS,
    MSG_FINALIZE_PROGRAM:    PORT_COMMANDS,
    MSG_THERMAL_PARAMS:     PORT_COMMANDS,
    MSG_PROTOCOL_STEP:   PORT_COMMANDS,
    # Results + load state
    MSG_GET_RESULT_DATA:     PORT_RESULTS,
    MSG_ACK_RESULT:          PORT_RESULTS,
    MSG_QUERY_RESULT_INFO:   PORT_RESULTS,
    MSG_QUERY_LOAD_STATE:    PORT_RESULTS,
    MSG_SET_PARAMETER:       PORT_RESULTS,
    # Status polling
    MSG_SUBSYS_STATUS:       PORT_STATUS,
    MSG_SUBSYS_QUERY:        PORT_STATUS,
    MSG_STATUS_QUERY:        PORT_STATUS,
    MSG_CALIBRATION:         PORT_STATUS,
    # SysInfo/CtrlInfo: on 5100 during handshake, on 5101 as logs
    MSG_SYS_INFO:            PORT_KEEPALIVE,
    MSG_CTRL_INFO:           PORT_KEEPALIVE,
}


class LightCyclerPacket:
    """Encode/decode the 4-byte-header + ASCII-payload wire format."""

    HEADER_SIZE = 4

    def __init__(self, msg_type: int, payload: bytes):
        self.msg_type = msg_type
        self.payload = payload

    def encode(self) -> bytes:
        return struct.pack('>HH', self.msg_type, len(self.payload)) + self.payload

    @classmethod
    def decode(cls, data: bytes) -> tuple[LightCyclerPacket | None, int]:
        """Decode one packet from a buffer. Returns (packet, bytes_consumed)."""
        if len(data) < cls.HEADER_SIZE:
            return None, 0
        msg_type, length = struct.unpack('>HH', data[:4])
        if len(data) < 4 + length:
            return None, 0
        return cls(msg_type, data[4:4 + length]), 4 + length

    @property
    def fields(self) -> list[str]:
        """Parse payload into newline-separated fields."""
        return self.payload.decode('ascii', errors='replace').rstrip('\n').split('\n')

    @property
    def type_name(self) -> str:
        return MSG_NAMES.get(self.msg_type, f"0x{self.msg_type:04X}")

    def __repr__(self):
        return f"Packet({self.type_name}, {len(self.payload)}B)"


class ResultData:
    """Parsed fluorescence result from a 0x044D response."""

    def __init__(self, fields: list[str]):
        self.status = int(fields[0])
        self.format_version = int(fields[1])
        self.data_type = int(fields[2])
        self.block_id = int(fields[3])
        self.guid = fields[4]
        self.program = int(fields[5])
        self.step = int(fields[6])
        self.acquisition = int(fields[7])
        self.time_centisecs = int(fields[8])
        self.temp_centideg = int(fields[9])
        self.exposure = int(fields[10])
        self.ref_channel = int(fields[11])
        self.reserved = int(fields[12])
        self.well_count = int(fields[13])
        self.values = [int(fields[14 + i]) for i in range(self.well_count)]

    @property
    def time_seconds(self) -> float:
        return self.time_centisecs / 100.0

    @property
    def temperature(self) -> float:
        return self.temp_centideg / 100.0


@dataclass
class InstrumentInfo:
    """Information returned by the port 5100 handshake."""
    controller_count: str = ''
    firmware_version: str = ''
    timestamp: str = ''
    event_state: list[str] | None = None
    hardware_info: list[str] | None = None
    controller_info: list[str] | None = None


class _PortSocket:
    """Manages one TCP socket with its own recv buffer and thread."""

    def __init__(self, port: int, host: str):
        self.port = port
        self.host = host
        self.sock: socket.socket | None = None
        self._recv_buf = b''
        self._send_lock = threading.Lock()

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(0.5)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def send(self, pkt: LightCyclerPacket):
        data = pkt.encode()
        with self._send_lock:
            self.sock.sendall(data)

    def recv_packets(self) -> list[LightCyclerPacket]:
        """Non-blocking: read available data and return decoded packets."""
        try:
            data = self.sock.recv(4096)
            if not data:
                return []
            self._recv_buf += data
        except socket.timeout:
            pass
        except Exception:
            return []

        packets = []
        while True:
            pkt, n = LightCyclerPacket.decode(self._recv_buf)
            if pkt is None:
                break
            self._recv_buf = self._recv_buf[n:]
            packets.append(pkt)
        return packets


class LightCyclerConnection:
    """Multi-port TCP client for the LightCycler 480 protocol.

    Opens 5 TCP connections matching the firmware's port architecture:
      5100 - Keepalive heartbeat + initial handshake
      5101 - Event/trace stream (auto-ACK)
      5102 - Experiment commands
      5104 - Result data queries
      5105 - Status polling
    """

    def __init__(self, host: str, port: int = 5100):
        self.host = host
        self.base_port = port
        self._sockets: dict[int, _PortSocket] = {}
        self._running = False
        # Event collection (for wait_for_event)
        self._events: list[list[str]] = []
        self._events_lock = threading.Lock()
        self._events_cond = threading.Condition(self._events_lock)
        # Command response matching
        self._resp_waiters: dict[int, threading.Event] = {}
        self._resp_packets: dict[int, LightCyclerPacket] = {}
        self._resp_lock = threading.Lock()
        # Instrument info from handshake
        self.info: InstrumentInfo | None = None

    def connect(self) -> InstrumentInfo:
        """Open all 5 connections, perform handshake, register for events.

        Returns InstrumentInfo with version/hardware details.
        """
        offsets = {
            PORT_KEEPALIVE: 0,
            PORT_EVENTS: 1,
            PORT_COMMANDS: 2,
            PORT_RESULTS: 4,
            PORT_STATUS: 5,
        }

        # Connect port 5100 first
        port_ka = self.base_port + offsets[PORT_KEEPALIVE]
        ps = _PortSocket(port_ka, self.host)
        log.info("Connecting to %s:%d (keepalive)", self.host, port_ka)
        ps.connect()
        self._sockets[PORT_KEEPALIVE] = ps

        self._running = True

        # Start keepalive immediately
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

        # Perform handshake on port 5100
        self.info = self._handshake()
        log.info("Handshake OK: %s v%s", self.info.controller_count,
                 self.info.firmware_version)

        # Connect remaining ports
        for canonical_port, offset in offsets.items():
            if canonical_port == PORT_KEEPALIVE:
                continue
            actual_port = self.base_port + offset
            ps = _PortSocket(actual_port, self.host)
            log.info("Connecting to %s:%d (%s)", self.host, actual_port,
                     {PORT_EVENTS: 'events', PORT_COMMANDS: 'commands',
                      PORT_RESULTS: 'results', PORT_STATUS: 'status'}[canonical_port])
            ps.connect()
            self._sockets[canonical_port] = ps

        # Start recv threads for all ports
        for port in ALL_PORTS:
            threading.Thread(target=self._recv_loop, args=(port,),
                             daemon=True).start()

        # Register on port 5101 for events
        self._register_events()

        return self.info

    def disconnect(self):
        self._running = False
        time.sleep(0.6)
        for ps in self._sockets.values():
            ps.close()
        self._sockets.clear()

    def command(self, msg_type: int, payload: bytes,
                timeout: float = 10.0) -> LightCyclerPacket:
        """Send a command and wait for its typed response."""
        pkt = LightCyclerPacket(msg_type, payload)
        evt = threading.Event()
        with self._resp_lock:
            self._resp_waiters[msg_type] = evt
            self._resp_packets.pop(msg_type, None)
        self._send(pkt)
        if not evt.wait(timeout):
            with self._resp_lock:
                self._resp_waiters.pop(msg_type, None)
            raise TimeoutError(
                f"No response for {MSG_NAMES.get(msg_type, hex(msg_type))}"
            )
        with self._resp_lock:
            return self._resp_packets.pop(msg_type)

    def query(self, msg_type: int, timeout: float = 10.0) -> LightCyclerPacket:
        """Send a single-space query and wait for response."""
        return self.command(msg_type, b' ', timeout)

    def wait_for_event(self, predicate, timeout: float = 300.0) -> list[str]:
        """Wait for and consume an event where predicate(fields) is True."""
        deadline = time.time() + timeout
        with self._events_lock:
            while True:
                for i, fields in enumerate(self._events):
                    try:
                        if predicate(fields):
                            self._events.pop(i)
                            return fields
                    except (IndexError, ValueError):
                        continue
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("Event wait timed out")
                self._events_cond.wait(timeout=min(remaining, 1.0))

    # --- Internal ---

    def _get_socket(self, port: int) -> _PortSocket:
        return self._sockets[port]

    def _port_for(self, msg_type: int) -> int:
        return _MSG_PORT.get(msg_type, PORT_COMMANDS)

    def _send(self, pkt: LightCyclerPacket):
        port = self._port_for(pkt.msg_type)
        ps = self._get_socket(port)
        if pkt.msg_type != MSG_KEEPALIVE:
            log.debug("TX [%d] %s (%dB)", port, pkt.type_name, len(pkt.payload))
        ps.send(pkt)

    def _send_on(self, port: int, pkt: LightCyclerPacket):
        """Send on a specific port (bypassing routing)."""
        ps = self._get_socket(port)
        ps.send(pkt)

    def _handshake(self) -> InstrumentInfo:
        """Perform the startup handshake on port 5100."""
        ps = self._get_socket(PORT_KEEPALIVE)
        info = InstrumentInfo()

        def _query_sync(msg_type: int, payload: bytes = b'?',
                        timeout: float = 5.0) -> LightCyclerPacket:
            ps.send(LightCyclerPacket(msg_type, payload))
            deadline = time.time() + timeout
            while time.time() < deadline:
                for pkt in ps.recv_packets():
                    if pkt.msg_type == msg_type:
                        return pkt
                time.sleep(0.05)
            raise TimeoutError(f"Handshake: no response for 0x{msg_type:04X}")

        # 0x0001 Version query
        resp = _query_sync(MSG_VERSION_QUERY)
        fields = resp.fields
        if len(fields) >= 3:
            info.controller_count = fields[0]
            info.firmware_version = fields[1]
            info.timestamp = fields[2]

        # 0x000E Event state query
        resp = _query_sync(MSG_EVENT)
        info.event_state = resp.fields

        # 0x0004 Status flag
        _query_sync(MSG_STATUS_4)

        # 0x000C Hardware info
        resp = _query_sync(MSG_SYS_INFO)
        info.hardware_info = resp.fields

        # 0x000D Controller info
        resp = _query_sync(MSG_CTRL_INFO)
        info.controller_info = resp.fields

        return info

    def _register_events(self):
        """Register on port 5101 with timestamp + connection ID."""
        ts = time.strftime("%d.%m.%y_%H.%M.%S_4")
        log.info("Registering for events: %s", ts)
        self._send_on(PORT_EVENTS,
                      LightCyclerPacket(MSG_VERSION_QUERY, ts.encode('ascii')))

    def _keepalive_loop(self):
        while self._running:
            try:
                ps = self._get_socket(PORT_KEEPALIVE)
                ps.send(LightCyclerPacket(MSG_KEEPALIVE, b'hi'))
            except Exception:
                if self._running:
                    log.exception("Keepalive send error")
                break
            time.sleep(1.0)

    def _recv_loop(self, port: int):
        ps = self._get_socket(port)
        while self._running:
            try:
                data = ps.sock.recv(4096)
                if not data:
                    log.warning("Connection closed on port %d", port)
                    break
                ps._recv_buf += data
            except socket.timeout:
                continue
            except Exception:
                if self._running:
                    log.exception("Recv error on port %d", port)
                break
            while True:
                pkt, n = LightCyclerPacket.decode(ps._recv_buf)
                if pkt is None:
                    break
                ps._recv_buf = ps._recv_buf[n:]
                self._dispatch(port, pkt)

    def _dispatch(self, port: int, pkt: LightCyclerPacket):
        if pkt.msg_type == MSG_KEEPALIVE:
            return  # Silently consume keepalive responses

        fields = pkt.fields

        if pkt.msg_type == MSG_EVENT:
            # Auto-ACK on the port it arrived on
            seq = fields[0] if fields else ''
            self._send_on(port,
                          LightCyclerPacket(MSG_EVENT, seq.encode('ascii')))
            # Store for wait_for_event
            with self._events_lock:
                self._events.append(fields)
                self._events_cond.notify_all()
            code = fields[2] if len(fields) > 2 else '?'
            extra = ' '.join(f.strip() for f in fields[3:5]) if len(fields) > 3 else ''
            log.info("Event %s %s", code, extra)

        elif pkt.msg_type == MSG_TRACE:
            seq = fields[0] if fields else ''
            self._send_on(port,
                          LightCyclerPacket(MSG_TRACE, seq.encode('ascii')))
            if len(fields) > 3:
                log.debug("Trace: %s", fields[3][:80])

        elif pkt.msg_type in (MSG_SYS_INFO, MSG_CTRL_INFO, MSG_LOG_ENTRY, MSG_LOG_ENTRY_2):
            # Logs on port 5101 - ACK with seq number
            seq = fields[0] if fields else ''
            self._send_on(port,
                          LightCyclerPacket(pkt.msg_type, seq.encode('ascii')))
            if len(fields) > 3:
                log.debug("Log[0x%04X]: %s", pkt.msg_type, fields[3][:80])

        else:
            # Response to a command/query
            with self._resp_lock:
                evt = self._resp_waiters.pop(pkt.msg_type, None)
                if evt:
                    self._resp_packets[pkt.msg_type] = pkt
                    evt.set()
                else:
                    log.warning("Unexpected response on port %d: %s",
                                port, pkt.type_name)
