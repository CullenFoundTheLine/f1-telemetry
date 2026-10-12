# PS5 F1 25 telemetry → CSV → speed graph

The provided `f1_25_udp.py` listens on UDP port 20777 and recognizes EA packet formats **2025** and **2026** (the 2026 Season Pack format in the screenshot). It reads packet ID 6 (Car Telemetry), selects the player's car via `m_playerCarIndex`, saves `time_s,speed_kmh,throttle,brake,steer,gear,rpm` CSV columns, and passes that CSV through `telemetry_graph.py` to save one speed-vs-time PNG per continuous segment. It does not identify or verify career driver/team names, detect complete laps, or claim that a packet was received until one actually arrives.

## PS5 configuration

Connect PS5 and your Mac to a mutually reachable LAN. Find the Mac's **local IPv4 address** in System Settings → Wi-Fi → Details → TCP/IP (or try Terminal `ipconfig getifaddr en0` for Wi-Fi). Example: `192.168.1.50`. Do not copy that example address; use your Mac's actual address.

In F1 25 → Telemetry Settings:

- UDP Telemetry: On
- UDP Broadcast Mode: Off
- UDP IP Address: your **Mac's LAN IPv4 address** (not the screenshot's `127.0.0.1`)
- UDP Port: `20777`
- UDP Send Rate: `60 Hz` as already set, or `20 Hz` for initial diagnostics
- UDP Format: `2026` as already set; 2025 is supported as well
- Your Telemetry: Restricted is fine for data from the car you are driving

Begin a driving session in Career Mode; menu/lobby UDP datagrams are not car-speed measurements.

## macOS Terminal

Download/extract this ZIP into a single directory; from Terminal `cd` into that directory.

```bash
python3 -m pip install matplotlib
python3 f1_25_udp.py --seconds 60
```

Drive for 60 seconds; the program saves CSV(s) and PNG(s) to `recordings/`. To run continuously, use `python3 f1_25_udp.py` and stop with Ctrl+C. If port 20777 is already in use by another telemetry app, stop that app or change both the PS5 and this script's `--port`.

Read a captured CSV again using the original program:

```bash
python3 telemetry_graph.py recordings/ACTUAL_FILE_NAME.csv --no-show
```

Replace `ACTUAL_FILE_NAME.csv` with the filename shown by the listener. The listener automatically creates the PNG, so this last command is optional.

## Diagnostics

- `Listening` = only the UDP socket is open; it does **not** prove PS5 packets are arriving.
- `First UDP datagram` = network delivery works; other packet IDs may arrive before Car Telemetry packets.
- `LIVE` = a decoded, validated player-car telemetry sample was recorded.
- If no packets arrive, verify the Mac IP, same LAN, port, local firewall, and network isolation. On a Mac, grant any incoming-connection permission for the Python interpreter without disabling the firewall.
- `unsupported UDP format` = the game's packet format does not match this decoder; set 2025 or 2026.
- The script starts a new CSV if game session time rewinds (flashback) or the session changes; it discards late out-of-order UDP packets so the graph does not include backward time jumps. One PNG is produced per valid continuous CSV segment.
- Real-PS5 connectivity has **not** been tested here; synthetic binary packet and localhost UDP tests are included in `test_f1_25_udp.py`. Run with `python3 -m unittest -v test_f1_25_udp.py`.

Source specification: https://forums.ea.com/blog/f1-games-game-info-hub-en/ea-sports%E2%84%A2-f1%C2%AE25-2026-season-pack-udp-specification/12187347
