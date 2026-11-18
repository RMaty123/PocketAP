# PocketAP

A simple single-file Wi-Fi hotspot for Windows, controlled from the terminal.

The Windows Mobile hotspot often refuses to turn on without an internet connection. PocketAP does not need internet, so you can create a network at any time. It creates a local network for connecting devices. The app only shares internet into it when you turn that on with `internet on`.

## Requirements

- Windows 11 and a Wi-Fi adapter that supports Wi-Fi Direct
- Python 3.11 or newer
- Internet access on first launch only, when the required packages are installed automatically

## Running

```
python pocketap.py
```

The hotspot starts right away as the network `PocketAP` with the password `123456789`. You can also set the name and password at launch:

```
python pocketap.py MyNetwork -p MyPassword123
```

The default password is easy to guess, so set your own for longer use.

## Commands

| Command | What it does |
| --- | --- |
| `status` | shows the hotspot status |
| `ssid NAME` | changes the network name |
| `password NEW_PASSWORD` | changes the password (8 to 63 ASCII characters) |
| `internet on\|off` | turns internet sharing on or off (off by default) |
| `stop` | pauses the hotspot |
| `start` | starts the hotspot again |
| `help` | shows the help |
| `exit` | turns off the hotspot and quits |

Changing the name, password, or internet sharing while running restarts the hotspot, and connected devices have to reconnect. Changes stay in memory only, so the next launch uses the default values again.

You can also quit with Ctrl+C or by closing the window.

## Internet sharing

The `internet on` command shares the computer's current internet connection into the hotspot using Internet Connection Sharing in Windows.

- Sharing requires administrator rights. The app asks for them when you turn it on and reopens in a new window.
- Windows can only share one connection at a time, so any other active sharing is turned off.
- If the computer switches to a different network, run `stop` and `start` to share the new one.
- If sharing fails, the hotspot keeps running as a local network without internet.

## Low latency

While the hotspot runs, the app turns off Wi-Fi power saving (on battery too) and background Wi-Fi network scanning, because both delay packets from clients. It restores the original settings when the hotspot stops.

For the lowest latency, these also help:

- Do not connect the computer to another Wi-Fi network and keep `internet off`. Otherwise the card switches between two channels.
- Turn off Bluetooth if you do not need it. Wi-Fi and Bluetooth often share one antenna.
- Keep Energy saver in Windows off, set the power mode to Best performance, and ideally stay plugged in.
- Turn off power saving modes and roaming in the Wi-Fi driver. For Realtek cards, in PowerShell run as administrator:
  `Set-NetAdapterAdvancedProperty -Name "Wi-Fi" -RegistryKeyword "LpsEn" -DisplayValue "Low"` and
  `Set-NetAdapterAdvancedProperty -Name "Wi-Fi" -RegistryKeyword "RegRoamLevel" -DisplayValue "1. Disable"`.
- On your phone, turn off battery saver and turn on game mode.

## Troubleshooting

- Check that Wi-Fi is on and Airplane mode is off.
- Turn off Mobile hotspot in Windows and Miracast, because they use the same Wi-Fi Direct.
- If Windows does not assign addresses to clients, the app asks for administrator rights and starts its own DHCP server.
