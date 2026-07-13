# Rademacher DuoFern Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A custom Home Assistant integration for **Rademacher DuoFern** roller shutters via the DuoFern USB stick (model 7000). If anyone has model 9000 please let me know if you experience troubles.

This integration communicates directly with the DuoFern USB stick using the native serial protocol — through local USB or a network serial server, with no cloud dependency.

## Supported Devices

| Device Type | Code | Description |
|-------------|------|-------------|
| RolloTron Standard | 0x40 | Roller shutter actuator |
| RolloTron Comfort | 0x41 | Roller shutter actuator (comfort) |
| RolloTron Pro | 0x42 | Roller shutter actuator (pro) |
| Additional types | 0x47, 0x49, 0x4B, 0x4C, 0x4E, 0x61, 0x70 | Various shutter actuators |

**USB Stick:** Rademacher DuoFern USB-Stick (VID: 0x0403, PID: 0x6001)

## Features

- Open / Close / Stop / Set Position for roller shutters
- Real-time position reporting (push-based, no polling)
- USB auto-discovery
- Network serial connections through `ser2net` (`socket://` and `rfc2217://`)
- Config flow with 2-step setup (connection + device registration)
- Options flow for adding/removing devices after setup
- Standalone CLI tools for testing and device pairing

## Installation

### HACS (Recommended)

1. Open HACS (HomeAssistant Custom Solution) in Home Assistant
2. Click the three dots menu (top right) and select **Custom repositories**
3. Add `https://github.com/MSchenkl/homeassistant-duofern` with category **Integration**
4. Search for "Rademacher DuoFern" and install
5. Restart Home Assistant

### Manual Installation

1. Copy the `custom_components/duofern/` folder to your Home Assistant config directory:
   ```
   /config/custom_components/duofern/
   ```
2. Restart Home Assistant

## Configuration

### Step 1: Connection

Go to **Settings > Devices & Services > Add Integration > DuoFern**

- **Serial Connection**: Select a local DuoFern USB stick (e.g., `/dev/ttyUSB0`) or enter a `ser2net` URL such as `socket://192.168.1.20:2000` or `rfc2217://192.168.1.20:2000`
- **System Code**: The 6-digit hex code of your USB stick (starts with `6F`, e.g., `6F1A2B`). This is the dongle serial number, found in your previous FHEM config or on the stick itself.

### Step 2: Paired Devices

Enter the 6-digit hex codes of your paired DuoFern devices, separated by commas:

```
e.g. 406B2D, 4090AE, 40B690, 
```

These are the device codes from your previous FHEM configuration.

### Managing Devices After Setup

Go to **Settings > Devices & Services > DuoFern > Configure** to add or remove device codes at any time. The integration will reload automatically.

### Using ser2net

The simplest setup is a raw TCP connection. A minimal `ser2net.yaml` entry on the machine connected to the USB stick is:

```yaml
connection: &duofern
  accepter: tcp,2000
  connector: serialdev,/dev/ttyUSB0,115200n81,local
  options:
    kickolduser: true
```

Then enter this connection in Home Assistant:

```text
socket://192.168.1.20:2000
```

For RFC2217, change the ser2net accepter to `telnet(rfc2217),tcp,2000` and use the corresponding URL instead:

```text
rfc2217://192.168.1.20:2000
```

Only one client should use the stick at a time. Replace the example address, port, and device path with values for your environment. Keep the connection on a trusted network; the examples do not add encryption or authentication.

## CLI Tools

The `tools/` directory contains standalone Python scripts for testing and device management without Home Assistant.

### Requirements

```bash
pip install pyserial pyserial-asyncio-fast
```

> **HAOS (Home Assistant OS) Note:** On HAOS the system Python is externally managed. You need to run:
> ```bash
> apk add py3-pip
> pip install --break-system-packages pyserial-asyncio-fast
> ```
> This is only needed for the CLI tools. The integration itself installs dependencies automatically via `manifest.json`.

### test_duofern.py — Test Script

Control roller shutters directly from the command line:

```bash
python3 tools/test_duofern.py 4053B8 up           # Open one shutter
python3 tools/test_duofern.py 4053B8 down          # Close one shutter
python3 tools/test_duofern.py 4053B8 stop          # Stop one shutter
python3 tools/test_duofern.py 4053B8 position 50   # Set one to 50%
python3 tools/test_duofern.py 4053B8 status        # Status of one device
python3 tools/test_duofern.py up                   # Open ALL shutters
python3 tools/test_duofern.py down                 # Close ALL shutters
python3 tools/test_duofern.py position 50          # Set ALL to 50%
python3 tools/test_duofern.py status               # Status of ALL devices
python3 tools/test_duofern.py statusall            # Broadcast status request
python3 tools/test_duofern.py --port socket://192.168.1.20:2000 statusall
```

### pair_duofern.py — Pairing Tool

Pair and unpair DuoFern devices without FHEM:

```bash
python3 tools/pair_duofern.py pair              # Start pairing (60s window)
python3 tools/pair_duofern.py unpair            # Start unpairing
python3 tools/pair_duofern.py list              # List all devices with status
python3 tools/pair_duofern.py pair --timeout 120 -v  # Extended timeout + debug
python3 tools/pair_duofern.py --port socket://192.168.1.20:2000 list
```

**Important:** The HA integration must be disabled while using CLI tools, as only one process can access the serial port at a time.

## Protocol

This integration implements the DuoFern serial protocol from scratch:

- **Frame format**: Fixed 22-byte (44 hex char) frames over UART at 115200 baud
- **Init sequence**: 7-step handshake (Init1, Init2, SetDongle, Init3, SetPairs, InitEnd, StatusBroadcast)
- **ACK-gated send queue**: One command in-flight at a time with 5-second timeout
- **Push-based status**: Devices report status changes proactively
- **Position convention**: DuoFern uses 0=open/100=closed, Home Assistant uses 0=closed/100=open (converted transparently)

Protocol implementation based on analysis of FHEM modules `10_DUOFERNSTICK.pm` and `30_DUOFERN.pm`.

## Migrating from other Environments

1. Note your system code and device codes from your old configuration
2. Install this integration and enter the codes during setup
3. Device pairing is preserved in the USB stick — no re-pairing needed
4. For new devices, you can always use `pair_duofern.py` 

## License

MIT License - see [LICENSE](LICENSE) for details.
