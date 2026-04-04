# LightCycler 480 II - Reverse-Engineered Protocol

Control a Roche LightCycler 480 II qPCR instrument over TCP.

## Network Setup

The LightCycler has a static IP of `192.168.95.41`. Your machine needs to be
on the same subnet. Connect via Ethernet and add an alias:

```bash
# Find your Ethernet interface (usually en10 for USB-Ethernet on Mac)
ifconfig | grep -B5 "status: active" | grep -E "^[a-z]|status"

# Add the alias
sudo ifconfig en10 alias 192.168.95.42 netmask 255.255.255.0

# Verify
ping 192.168.95.41
```

To remove later: `sudo ifconfig en10 -alias 192.168.95.42`

## Quick Start

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Mock mode (no instrument needed)
python server.py --mock
python client.py state

# Real instrument
python server.py --host 192.168.95.41
python client.py run --temp 37 --hold 1 --reads 3
python client.py experiment <GUID>
```

## Project Structure

```
lightcycler-demo/
├── lightcycler.py       # Core TCP protocol (5-port architecture)
├── experiment.py        # Stage/Step experiment builder (pylabrobot-compatible)
├── mock_lc.py           # Mock instrument for testing
├── server/
│   ├── proto/           # Protobuf service definition
│   ├── generated/       # buf codegen output
│   ├── server.py        # Connect RPC server + SQLite logging
│   ├── client.py        # CLI client
│   └── schema.sql       # SQLite schema
├── examples/
│   └── run_experiment.py
└── data/                # Wireshark captures, logs, protocol notes
```

## Physical Setup

The plate tray must be loaded manually (physical button on the machine).
The machine must reach state 10 (idle) before experiments can run.
A SwitchBot Fingerbot can automate the door button — pass `--fingerbot-mac`
to the server.
