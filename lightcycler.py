#!/usr/bin/env python3
"""LightCycler 480 II - TCP Protocol Implementation

Wire format: [2B msg_type big-endian][2B payload_len big-endian][payload ASCII]
"""
from __future__ import annotations

import struct
import socket
import threading
import logging
import time

log = logging.getLogger(__name__)

# --- Message Types ---
MSG_KEEPALIVE           = 0x0000
MSG_EVENT               = 0x000E
MSG_TRACE               = 0x0010
MSG_STATUS_QUERY        = 0x0033
MSG_START_RUN           = 0x00C9
MSG_CREATE_EXPERIMENT   = 0x012D
MSG_FINALIZE_EXPERIMENT = 0x012E
MSG_DEFINE_PROGRAM      = 0x012F
MSG_FINALIZE_PROGRAM    = 0x0130
MSG_STEP_PARAMETERS     = 0x0131
MSG_ACQUISITION_POINT   = 0x0132
MSG_GET_RESULT_DATA     = 0x044D
MSG_ACK_RESULT          = 0x044E
MSG_QUERY_RESULT_INFO   = 0x044F
MSG_QUERY_LOAD_STATE    = 0x047F
MSG_SET_PARAMETER       = 0x04B0

MSG_NAMES = {
    0x0000: "Keepalive",       0x000E: "Event",
    0x0010: "Trace",           0x0033: "StatusQuery",
    0x00C9: "StartRun",        0x012D: "CreateExperiment",
    0x012E: "FinalizeExpt",    0x012F: "DefineProgram",
    0x0130: "FinalizeProgram", 0x0131: "StepParameters",
    0x0132: "AcquisitionPt",   0x044D: "GetResultData",
    0x044E: "AckResult",       0x044F: "QueryResultInfo",
    0x047F: "QueryLoadState",  0x04B0: "SetParameter",
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
    """Parsed fluorescence result from a 0x044D response.

    Fields: status, format_version(1), data_type(2), block_id(1), GUID,
            program, step, acquisition, time_centisecs, temp_centideg,
            exposure(389949), ref_channel, reserved(0), well_count,
            <well_count fluorescence values>
    """

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


class LightCyclerConnection:
    """TCP client for the LightCycler 480 protocol.

    - Keepalive thread sends "hi" every 1s
    - Recv thread auto-ACKs 0x000E and 0x0010 messages
    - Events are collected for wait_for_event() pattern matching
    - command()/query() send and wait for typed responses
    """

    def __init__(self, host: str, port: int = 5100):
        self.host = host
        self.port = port
        self.sock = None
        self._recv_buf = b''
        self._send_lock = threading.Lock()
        self._running = False
        # Event collection (for wait_for_event)
        self._events: list[list[str]] = []
        self._events_lock = threading.Lock()
        self._events_cond = threading.Condition(self._events_lock)
        # Command response matching
        self._resp_waiters: dict[int, threading.Event] = {}
        self._resp_packets: dict[int, LightCyclerPacket] = {}
        self._resp_lock = threading.Lock()

    def connect(self):
        log.info("Connecting to %s:%d", self.host, self.port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(0.5)
        self._running = True
        threading.Thread(target=self._recv_loop, daemon=True).start()
        threading.Thread(target=self._keepalive_loop, daemon=True).start()
        log.info("Connected")

    def disconnect(self):
        self._running = False
        time.sleep(0.6)
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

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
        """Wait for and consume an event where predicate(fields) is True.

        Events are stored in insertion order. The first matching event is
        removed and returned; non-matching events remain for future waits.
        """
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

    def _send(self, pkt: LightCyclerPacket):
        data = pkt.encode()
        if pkt.msg_type != MSG_KEEPALIVE:
            log.debug("TX %s (%dB)", pkt.type_name, len(data))
        with self._send_lock:
            self.sock.sendall(data)

    def _keepalive_loop(self):
        while self._running:
            try:
                self._send(LightCyclerPacket(MSG_KEEPALIVE, b'hi'))
            except Exception:
                if self._running:
                    log.exception("Keepalive send error")
                break
            time.sleep(1.0)

    def _recv_loop(self):
        while self._running:
            try:
                data = self.sock.recv(4096)
                if not data:
                    log.warning("Connection closed by LC")
                    self._running = False
                    break
                self._recv_buf += data
            except socket.timeout:
                continue
            except Exception:
                if self._running:
                    log.exception("Recv error")
                break
            self._process_buffer()

    def _process_buffer(self):
        while True:
            pkt, n = LightCyclerPacket.decode(self._recv_buf)
            if pkt is None:
                break
            self._recv_buf = self._recv_buf[n:]
            self._dispatch(pkt)

    def _dispatch(self, pkt: LightCyclerPacket):
        if pkt.msg_type == MSG_KEEPALIVE:
            return  # Silently consume keepalive responses

        fields = pkt.fields

        if pkt.msg_type == MSG_EVENT:
            # Auto-ACK: echo the sequence number back
            seq = fields[0] if fields else ''
            self._send(LightCyclerPacket(MSG_EVENT, seq.encode('ascii')))
            # Store for wait_for_event
            with self._events_lock:
                self._events.append(fields)
                self._events_cond.notify_all()
            # Log the interesting part
            code = fields[2] if len(fields) > 2 else '?'
            extra = ' '.join(f.strip() for f in fields[3:5]) if len(fields) > 3 else ''
            log.info("Event %s %s", code, extra)

        elif pkt.msg_type == MSG_TRACE:
            seq = fields[0] if fields else ''
            self._send(LightCyclerPacket(MSG_TRACE, seq.encode('ascii')))
            if len(fields) > 3:
                log.debug("Trace: %s", fields[3][:80])

        else:
            # Response to a command/query
            with self._resp_lock:
                evt = self._resp_waiters.pop(pkt.msg_type, None)
                if evt:
                    self._resp_packets[pkt.msg_type] = pkt
                    evt.set()
                else:
                    log.warning("Unexpected response: %s", pkt.type_name)
