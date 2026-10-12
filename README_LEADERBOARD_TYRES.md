# F1 25 — Leaderboard + tyre tracking 🏎️

A **small add-on** to your existing VS Code project. It keeps your original Python scripts and recordings and adds a leaderboard and lap-by-lap tyre history. Designed for **F1 25 on PS5, UDP format 2026**.

## Add it to your current folder

Copy these three files next to `f1_25_udp_v2.py` (don't delete or overwrite anything):

- `f1_25_monitor.py` — records live leaderboard snapshots, tyres and completed laps.
- `tyre_compare.py` — compares your recorded valid laps by tyre compound.
- `test_f1_25_monitor.py` — tests the add-on with synthetic packets.

Keep `f1_25_udp.py`, `f1_25_udp_v2.py`, `telemetry_graph.py`, and `recordings/` exactly where they are. No additional Python packages are needed for the new CSV files; your existing graph still uses Matplotlib.

## Record in practice, qualifying or a race

In VS Code's Terminal, with the project folder open:

```bash
python3 f1_25_monitor.py --seconds 600
```

Drive on PS5. You'll see a brief top-five leaderboard in Terminal roughly every 10 game seconds. Stop early with **Ctrl+C**. **Run only one listener** at a time: stop `f1_25_udp_v2.py` before starting this one. Your PS5's UDP port stays `20777` and UDP format stays `2026`.

The monitor also calls your existing V2 recorder: the original full telemetry, pit-event CSV, on-track stint CSVs and speed graphs are still generated. New output files appear in `recordings/`:

| New file suffix | What's inside |
| --- | --- |
| `_leaderboard.csv` | Game-reported position for each active car, driver name when provided, best **captured valid lap** and its separate calculated rank, time gaps when supplied, and each car's reported tyres. One snapshot per game second. |
| `_laps.csv` | Completed laps, game-reported lap time, valid/invalid flag, tyre used **on that lap** from Session History, tyre stint, car and name. |
| `_tyres.csv` | Your fitted compound and tyre age each second; fitted-set wear/life **only when available**. |

**Position is session-dependent:** P1 in practice/qualifying is classification, not a race lead or pole award; in a race it is the current race position. `best_lap_rank` is only the ranking of valid laps our program has received, *not* an official classification. Names/team IDs come from the game; if they're missing, the car index is retained. A blank means we don't have a reliable value.

## Which tyres actually helped your laps?

Run this after your recording, replacing the name with the actual `_laps.csv` file shown in the VS Code Explorer:

```bash
python3 tyre_compare.py recordings/YOUR_SESSION_laps.csv
```

It prints the **number of valid completed laps, fastest lap and median lap** for Soft, Medium, Hard and wet tyres separately. Your reported **1:21 on Hard vs 1:20 on Soft** suggests a roughly 1-second best-lap difference in that session, but three Hard laps versus one Soft lap don't isolate tyre effects. A fairer comparison uses the same track, setup, fuel, weather and similar tyre ages across multiple clean laps. The program never guesses tyre compounds for old seven-column CSVs.

## Test / troubleshoot

```bash
python3 -m unittest -v test_f1_25_monitor.py
```

If the original graph records but `_leaderboard.csv` has blank names, give the game a few seconds to send Participants packets. If tyre fields stay blank, check for valid Car Status / Session History packets and verify the PS5 UDP format is `2026`. If the game has a newer, incompatible packet layout, the parser reports a rejected packet instead of making up results. **Tests use synthetic packets; a fresh PS5 session is needed to verify live capture.**

EA's [F1 25 / 2026 Season Pack UDP specification](https://forums.ea.com/blog/f1-games-game-info-hub-en/ea-sports%E2%84%A2-f1%C2%AE25-2026-season-pack-udp-specification/12187347) describes Participants (4), Lap Data (2), Car Status (7), Session History (11) and Tyre Sets (12).
