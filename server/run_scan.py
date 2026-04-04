#!/usr/bin/env python3
"""Scan for SwitchBot BLE devices."""

import asyncio
from bleak import BleakScanner


async def scan(timeout: float = 10):
    print(f"Scanning for BLE devices ({timeout}s)...")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)

    SWITCHBOT_MANUFACTURER_ID = 2409  # 0x0969
    switchbot_devices = [
        (d, adv) for d, adv in devices.values()
        if SWITCHBOT_MANUFACTURER_ID in adv.manufacturer_data
        or (d.name and ("switchbot" in d.name.lower() or "bot" in d.name.lower()))
    ]

    if switchbot_devices:
        for d, adv in switchbot_devices:
            print(f"  {d.address}  {d.name}  rssi={adv.rssi}")
    else:
        print("No SwitchBot devices found by name. All BLE devices (verbose):")
        ranked = sorted(devices.values(), key=lambda pair: pair[1].rssi or -100, reverse=True)
        for d, adv in ranked[:20]:
            print(f"  {d.address}  {d.name or '(unknown)'}  rssi={adv.rssi}")
            if adv.service_uuids:
                print(f"    service_uuids: {adv.service_uuids}")
            if adv.manufacturer_data:
                print(f"    manufacturer_data: {dict(adv.manufacturer_data)}")
            if adv.local_name:
                print(f"    local_name: {adv.local_name}")


if __name__ == "__main__":
    asyncio.run(scan())
