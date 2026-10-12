# F1 25 Telemetry 🏎️

**PS5 → Mac → CSV → speed graph.** Record a drive, separate pit activity from on-track data, and explore your laps. Your original files and recordings stay put.

## 1. PS5 setup

Put your PS5 and Mac on the same reachable network. In **F1 25 → Telemetry Settings**:

| Setting | Value |
| --- | --- |
| UDP Telemetry | On |
| Broadcast Mode | Off |
| IP Address | Your Mac's **local IPv4**, not `127.0.0.1` |
| Port | `20777` |
| Send Rate | `60 Hz` |
| UDP Format | `2026` |
| Your Telemetry | Restricted is fine for your car |

Find your Mac's address in **System Settings → Wi-Fi → Details → TCP/IP** or with `ipconfig getifaddr en0` in Terminal.

## 2. Let's record

Open your existing `f1_25_udp_starter` folder in VS Code, then **Terminal → New Terminal**. Keep `f1_25_udp.py`, `telemetry_graph.py`, and `f1_25_udp_v2.py` together.

```bash
python3 f1_25_udp_v2.py --seconds 180
```

Start **in the garage**, exit the pits, and drive. Recording ends after three minutes, or press **Ctrl+C** to finish early. Omit `--seconds 180` to record until you stop. Run only **one** listener on port `20777` at a time.

If needed, install the graphing library with `python3 -m pip install matplotlib`.

## 3. Your recordings

Look in `recordings/`:

| File ending | What's inside |
| --- | --- |
| `_all.csv` | Full, **untrimmed** telemetry, including pit activity. |
| `_events.csv` | Observed pit transitions and available pit timers. |
| `_ontrack_stint01.csv` | On-track samples, with time reset to the stint start. |
| `_ontrack_stint01_speed_vs_time.png` | Your on-track speed graph. |

Later stints use `stint02`, etc. Flashbacks and session changes create a new `partNN`; earlier files remain intact.

## 4. Read the data

Along with speed and pedal inputs, the upgraded CSV captures available **lap number, lap distance, lap time, race position, gaps, and track coordinates**. A blank field means that measurement wasn't available—not zero.

**Grid position** is where you started; **race position** is where you are now. The reported gap ahead or behind can show whether another car is closing or falling back.

**Pit timing matters:** garage-to-exit time includes waiting, so it isn't the same as pit-lane time. Pause time isn't measured. Your uploaded session starts with `on_track_first_observed`, so it **cannot establish your earlier pit-exit duration**. Record from the garage next time to catch that transition.

## 5. Quick fixes

**No packets?** Check the Mac IP, PS5 target IP, network, and port `20777`. **No on-track graph?** Confirm UDP Format `2026` and drive on track so the listener receives Lap Data.

**Keep the raw CSV.** We can always analyze it again without losing the original drive. 🏁
