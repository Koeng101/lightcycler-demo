#!/usr/bin/env python3
"""LightCycler 480 II - Experiment Controller CLI

Drives the full experiment lifecycle: connect, wait for init, load plate,
define experiment, run, collect fluorescence data, save CSV.

Usage:
    python run_experiment.py --mock --temp 37 --hold 1 --acquisitions 3 -o test.csv
    python run_experiment.py --host 192.168.95.41 --temp 37 --hold 1 --acquisitions 3
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import threading
import time

from lightcycler import (
    LightCyclerConnection, ResultData,
    MSG_STATUS_QUERY, MSG_QUERY_LOAD_STATE, MSG_SET_PARAMETER,
    MSG_GET_RESULT_DATA, MSG_ACK_RESULT, MSG_QUERY_RESULT_INFO,
)
from experiment import ExperimentConfig, Program, Acquisition, build_commands

log = logging.getLogger(__name__)

FILTER_MAP = {
    'sybr_green': 1, 'sybr': 1, 'fam': 1,
    'hex': 2, 'vic': 2,
    'rox': 3,
    'cy5': 4,
}


def run_experiment(conn: LightCyclerConnection,
                   config: ExperimentConfig,
                   output_path: str | None) -> list[ResultData] | None:
    """Execute the full experiment sequence on an active connection.

    Phases match the captured protocol trace timing:
      0. Connect (already done)
      1. Wait for initialization complete
      2. Wait for plate loading
      3. Pre-experiment queries
      4. Send experiment definition
      5. Monitor run, collect data
      6. Post-run cleanup
    """
    commands = build_commands(config)
    num_acq = sum(len(p.acquisitions) for p in config.programs)

    # --- Phase 1: Initialization (~61s on real instrument) ---
    log.info("Phase 1: Waiting for initialization...")
    conn.wait_for_event(
        lambda f: (len(f) > 4
                   and f[2] == '90'
                   and f[3].strip() == '11'
                   and f[4].strip() == '100'),
        timeout=120,
    )
    log.info("Initialization complete")

    # --- Phase 2: Plate loading ---
    log.info("Phase 2: Waiting for plate loading...")
    conn.wait_for_event(lambda f: len(f) > 2 and f[2] == '303', timeout=120)
    log.info("Plate loaded")

    # Immediate load state query after plate detection
    conn.query(MSG_QUERY_LOAD_STATE)

    # Wait for instrument to reach idle state
    conn.wait_for_event(
        lambda f: (len(f) > 3
                   and f[2] == '11'
                   and f[3].strip() == '10'),
        timeout=60,
    )
    log.info("Instrument idle")

    # --- Phase 3: Pre-experiment queries ---
    log.info("Phase 3: Pre-experiment queries")
    conn.query(MSG_QUERY_LOAD_STATE)
    conn.command(MSG_SET_PARAMETER, b'15')
    conn.query(MSG_STATUS_QUERY)
    time.sleep(1)
    conn.query(MSG_STATUS_QUERY)

    # --- Phase 4: Experiment definition ---
    log.info("Phase 4: Sending experiment definition (%d commands)", len(commands))
    for msg_type, payload in commands:
        resp = conn.command(msg_type, payload)
        status = resp.fields[0] if resp.fields else '?'
        if status != '0':
            log.error("Command 0x%04X failed: %s", msg_type,
                      ' '.join(resp.fields))
            return None
    log.info("Experiment started")

    # --- Phase 5: Run monitoring ---
    log.info("Phase 5: Monitoring run...")

    # Wait for run started (event 104)
    conn.wait_for_event(
        lambda f: len(f) > 2 and f[2] == '104', timeout=30)
    log.info("Run started, waiting for warm-up...")

    # Wait for warm-up complete (event 90, sub=12, progress=100)
    conn.wait_for_event(
        lambda f: (len(f) > 4
                   and f[2] == '90'
                   and f[3].strip() == '12'
                   and f[4].strip() == '100'),
        timeout=120,
    )
    log.info("Warm-up complete, acquiring data...")

    # Collect each acquisition
    results = []
    for i in range(1, num_acq + 1):
        # Wait for data ready (event 201)
        conn.wait_for_event(
            lambda f: len(f) > 2 and f[2] == '201', timeout=60)
        log.info("Acquisition %d/%d: fetching data...", i, num_acq)

        # Fetch result data (0x044D)
        resp = conn.command(
            MSG_GET_RESULT_DATA, f"{i}\n".encode('ascii'))
        result = ResultData(resp.fields)
        results.append(result)

        # Query result info (0x044F) and acknowledge (0x044E)
        conn.query(MSG_QUERY_RESULT_INFO)
        conn.command(MSG_ACK_RESULT, str(i).encode('ascii'))

        mean_fl = sum(result.values) / len(result.values)
        log.info("  T=%.2f C  t=%.1fs  mean_fluor=%.1f  ref=%d",
                 result.temperature, result.time_seconds,
                 mean_fl, result.ref_channel)

    # Wait for run complete (event 105)
    conn.wait_for_event(
        lambda f: len(f) > 2 and f[2] == '105', timeout=60)
    log.info("Run complete")

    # --- Phase 6: Post-run ---
    log.info("Phase 6: Post-run cleanup")

    # Wait for success status (event 101 with value 0)
    conn.wait_for_event(
        lambda f: (len(f) > 3
                   and f[2] == '101'
                   and f[3].strip() == '0'),
        timeout=30,
    )

    # Wait for return to idle (event 11 with state 10)
    conn.wait_for_event(
        lambda f: (len(f) > 3
                   and f[2] == '11'
                   and f[3].strip() == '10'),
        timeout=30,
    )

    # Post-run queries (matches captured sequence)
    conn.query(MSG_STATUS_QUERY)
    conn.query(MSG_STATUS_QUERY)
    conn.command(MSG_SET_PARAMETER, b'15')

    # --- Write results ---
    if output_path and results:
        write_csv(output_path, results)

    log.info("Experiment finished successfully")
    return results


def write_csv(path: str, results: list[ResultData]):
    """Write fluorescence data to CSV."""
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'well', 'acquisition', 'temperature_c', 'time_s',
            'fluorescence', 'ref_channel',
        ])
        for r in results:
            for w in range(r.well_count):
                writer.writerow([
                    w + 1,
                    r.acquisition,
                    f"{r.temperature:.2f}",
                    f"{r.time_seconds:.2f}",
                    r.values[w],
                    r.ref_channel,
                ])
    log.info("Results written to %s (%d wells x %d acquisitions)",
             path, results[0].well_count, len(results))


def main():
    parser = argparse.ArgumentParser(
        description='LightCycler 480 II Experiment Controller',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('--host', default='192.168.95.41',
                        help='LightCycler IP address')
    parser.add_argument('--port', type=int, default=5100)
    parser.add_argument('--temp', type=float, default=37.0,
                        help='Target temperature (C)')
    parser.add_argument('--hold', type=float, default=1.0,
                        help='Hold time per acquisition (seconds)')
    parser.add_argument('--acquisitions', type=int, default=3,
                        help='Number of acquisitions')
    parser.add_argument('--cycles', type=int, default=1)
    parser.add_argument('--wells', type=int, default=384,
                        choices=[96, 384])
    parser.add_argument('--volume', type=int, default=20,
                        help='Reaction volume (uL)')
    parser.add_argument('--filter', default='sybr_green',
                        choices=list(FILTER_MAP.keys()))
    parser.add_argument('--output', '-o', default='results.csv',
                        help='Output CSV path')
    parser.add_argument('--mock', action='store_true',
                        help='Use built-in mock simulator')
    parser.add_argument('--mock-speed', type=float, default=50.0,
                        help='Mock simulation speed multiplier')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Enable debug logging')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-5s %(message)s',
        datefmt='%H:%M:%S',
    )

    # Build experiment config
    config = ExperimentConfig(
        well_count=args.wells,
        volume=args.volume,
        programs=[Program(
            cycles=args.cycles,
            acquisitions=[
                Acquisition(
                    target_temp=args.temp,
                    hold_time=args.hold,
                    filter_set=FILTER_MAP[args.filter],
                )
                for _ in range(args.acquisitions)
            ],
        )],
    )

    log.info("Experiment: %.1f C, %.1fs hold, %d acquisitions, "
             "%d wells, filter=%s",
             args.temp, args.hold, args.acquisitions,
             args.wells, args.filter)
    log.info("GUID: %s", config.guid)

    # Start mock simulator if requested
    mock = None
    if args.mock:
        from mock_lc import MockLightCycler
        mock = MockLightCycler(port=args.port, speed=args.mock_speed)
        mock.start()
        threading.Thread(target=mock.serve_one, daemon=True).start()
        args.host = '127.0.0.1'
        time.sleep(0.3)  # Let server start

    # Connect and run
    conn = LightCyclerConnection(args.host, args.port)
    try:
        conn.connect()
        results = run_experiment(conn, config, args.output)
        sys.exit(0 if results else 1)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        sys.exit(130)
    except Exception:
        log.exception("Fatal error")
        sys.exit(1)
    finally:
        conn.disconnect()
        if mock:
            mock.stop()


if __name__ == '__main__':
    main()
