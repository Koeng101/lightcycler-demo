#!/usr/bin/env python3
"""Test SwitchBot Fingerbot press."""

import argparse
import asyncio
from bleak import BleakScanner
from switchbot.devices.bot import Switchbot


async def run(mac: str):
    print(f"Connecting to {mac}...")
    dev = await BleakScanner.find_device_by_address(mac, timeout=10)
    if not dev:
        print(f"Device {mac} not found")
        return
    bot = Switchbot(dev)
    print("Pressing...")
    await bot.turn_on()
    print("Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mac", default="BCCC640D-19A2-E6B3-0269-2A9D0C54A9D6")
    args = parser.parse_args()
    asyncio.run(run(args.mac))
