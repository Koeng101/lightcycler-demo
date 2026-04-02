#!/usr/bin/env python3
"""LightCycler Connect RPC Client

Usage:
    python client.py state
    python client.py logs [--limit 50] [--since 0]
    python client.py run [--temp 37] [--hold 1] [--acquisitions 3]
    python client.py experiment <GUID>
    python client.py experiments
    python client.py door open|close|status
    python client.py scan  (BLE scan for SwitchBot devices)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'generated'))

import lightcycler_pb2 as pb
from lightcycler_connect import LightCyclerServiceClientSync


def get_client(address: str) -> LightCyclerServiceClientSync:
    return LightCyclerServiceClientSync(address=address)


def cmd_state(client: LightCyclerServiceClientSync, args):
    resp = client.get_state(pb.GetStateRequest())
    state_names = {6: 'post-init', 7: 'running', 10: 'idle'}
    state_str = state_names.get(resp.instrument_state, str(resp.instrument_state))
    print(f"Connected:  {resp.connected}")
    print(f"State:      {state_str} ({resp.instrument_state})")
    print(f"Firmware:   {resp.firmware_version}")
    print(f"Controller: {resp.controller_count}")
    if resp.current_experiment_guid:
        print(f"Running:    {resp.current_experiment_guid}")


def cmd_logs(client: LightCyclerServiceClientSync, args):
    resp = client.get_logs(pb.GetLogsRequest(
        limit=args.limit, since_id=args.since))
    for entry in resp.entries:
        print(f"[{entry.id:>5}] {entry.timestamp}  code={entry.event_code}"
              f"  {entry.sub_code}  {entry.message}")
    if not resp.entries:
        print("No log entries.")


def cmd_run(client: LightCyclerServiceClientSync, args):
    filter_map = {'sybr': 1, 'hex': 2, 'rox': 3, 'cy5': 4}
    resp = client.run_experiment(pb.RunExperimentRequest(
        temp_c=args.temp,
        hold_time_s=args.hold,
        num_acquisitions=args.acquisitions,
        filter=filter_map.get(args.filter, 1),
        well_count=args.wells,
        volume_ul=args.volume,
    ))
    print(f"Experiment submitted: {resp.guid}")
    print(f"Poll with: python client.py experiment {resp.guid}")


def cmd_experiment(client: LightCyclerServiceClientSync, args):
    resp = client.get_experiment(pb.GetExperimentRequest(guid=args.guid))
    status_names = {1: 'pending', 2: 'running', 3: 'complete', 4: 'error'}
    print(f"GUID:       {resp.guid}")
    print(f"Status:     {status_names.get(resp.status, '?')}")
    print(f"Created:    {resp.created_at}")
    if resp.completed_at:
        print(f"Completed:  {resp.completed_at}")
    if resp.error_message:
        print(f"Error:      {resp.error_message}")
    if resp.config:
        print(f"Config:     {resp.config.temp_c}°C, {resp.config.hold_time_s}s hold, "
              f"{resp.config.num_acquisitions} acq, {resp.config.well_count} wells")
    for acq in resp.acquisitions:
        vals = list(acq.well_values)
        mean = sum(vals) / len(vals) if vals else 0
        print(f"  Acq {acq.acquisition_num}: T={acq.temperature_c:.2f}°C "
              f"t={acq.time_s:.1f}s mean={mean:.1f} ref={acq.ref_channel} "
              f"wells={len(vals)}")


def cmd_experiments(client: LightCyclerServiceClientSync, args):
    resp = client.list_experiments(pb.ListExperimentsRequest(limit=args.limit))
    status_names = {1: 'pending', 2: 'running', 3: 'complete', 4: 'error'}
    for exp in resp.experiments:
        print(f"{exp.guid}  {status_names.get(exp.status, '?'):>8}  "
              f"{exp.created_at}  acq={exp.num_acquisitions}")
    if not resp.experiments:
        print("No experiments.")


def cmd_door(client: LightCyclerServiceClientSync, args):
    if args.action == 'open':
        resp = client.open_door(pb.OpenDoorRequest())
        print(f"{'OK' if resp.success else 'FAILED'}"
              + (f": {resp.error}" if resp.error else ""))
    elif args.action == 'close':
        resp = client.close_door(pb.CloseDoorRequest())
        print(f"{'OK' if resp.success else 'FAILED'}"
              + (f": {resp.error}" if resp.error else ""))
    elif args.action == 'status':
        resp = client.get_door_status(pb.GetDoorStatusRequest())
        print(f"Available: {resp.available}")
        print(f"Battery:   {resp.battery_percent}%")
        if resp.error:
            print(f"Error:     {resp.error}")


def cmd_scan(client, args):
    """BLE scan for nearby SwitchBot devices."""
    async def _scan():
        from bleak import BleakScanner
        print("Scanning for BLE devices (10s)...")
        devices = await BleakScanner.discover(timeout=10)
        switchbot_devices = [d for d in devices
                             if d.name and 'switchbot' in d.name.lower()
                             or (d.name and 'bot' in d.name.lower())]
        if switchbot_devices:
            for d in switchbot_devices:
                print(f"  {d.address}  {d.name}  rssi={d.rssi}")
        else:
            print("No SwitchBot devices found. All BLE devices:")
            for d in sorted(devices, key=lambda d: d.rssi or -100, reverse=True)[:20]:
                print(f"  {d.address}  {d.name or '(unknown)'}  rssi={d.rssi}")
    asyncio.run(_scan())


def main():
    parser = argparse.ArgumentParser(description='LightCycler RPC Client')
    parser.add_argument('--url', default='http://localhost:8080',
                        help='Server URL')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('state', help='Get instrument state')

    p_logs = sub.add_parser('logs', help='Get event logs')
    p_logs.add_argument('--limit', type=int, default=50)
    p_logs.add_argument('--since', type=int, default=0)

    p_run = sub.add_parser('run', help='Run experiment')
    p_run.add_argument('--temp', type=float, default=37.0)
    p_run.add_argument('--hold', type=float, default=1.0)
    p_run.add_argument('--acquisitions', type=int, default=3)
    p_run.add_argument('--filter', default='sybr', choices=['sybr', 'hex', 'rox', 'cy5'])
    p_run.add_argument('--wells', type=int, default=384, choices=[96, 384])
    p_run.add_argument('--volume', type=int, default=20)

    p_exp = sub.add_parser('experiment', help='Get experiment by GUID')
    p_exp.add_argument('guid')

    p_list = sub.add_parser('experiments', help='List experiments')
    p_list.add_argument('--limit', type=int, default=50)

    p_door = sub.add_parser('door', help='Door control')
    p_door.add_argument('action', choices=['open', 'close', 'status'])

    sub.add_parser('scan', help='BLE scan for SwitchBot devices')

    args = parser.parse_args()
    client = get_client(args.url)

    cmds = {
        'state': cmd_state, 'logs': cmd_logs, 'run': cmd_run,
        'experiment': cmd_experiment, 'experiments': cmd_experiments,
        'door': cmd_door, 'scan': cmd_scan,
    }
    cmds[args.command](client, args)


if __name__ == '__main__':
    main()
