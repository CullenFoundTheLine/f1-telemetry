# F1 25 (2026 Season Pack) — pit timing + lap, race position and gaps

**The first seven-column CSV is preserved.** The uploaded historic recording cannot provide a verified pit exit or rival gaps because it has no Lap Data packet fields. Its 80 km/h plateau is only a speed-based clue. For the historic file, `trim_pit_csv.py` makes a *separate*, explicitly estimated graph and JSON; the original CSV stays intact.

## Install and run on your Mac

1. Unzip these files together in your `f1_25_udp_starter` folder (or use this ZIP as its own folder). Keep all `.py` files side by side. The older `f1_25_udp.py` is retained, unchanged.
2. In F1 25 PS5 telemetry settings: UDP **On**, UDP IP = your Mac's LAN IP, port **20777**, UDP Format **2026**, broadcast Off. Keep PS5 and Mac on a reachable local network.
3. Open Terminal in the extracted folder. You already have Matplotlib working, so this install is only needed if `import matplotlib` fails:

   ```bash
   python3 -m pip install matplotlib
   ```

4. Start a *fresh, long recording while still in the garage* so the first pit-to-track transition is observed:

   ```bash
   python3 f1_25_udp_v2.py --seconds 180
   ```

   Leave garage, drive your out lap and a timed lap. End early with `Ctrl+C`. For continuous capture, omit `--seconds 180`. Only one UDP listener can use port 20777 at once; stop the older `f1_25_udp.py` first.

## Outputs in `recordings/`

- `..._part01_all.csv`: **untrimmed, 2026 player-car telemetry**, plus matching Lap Data fields (lap number, lap distance, total distance, current and previous lap time, sector, lap validity, grid start position, live race position, pit/driver status, reported pit timers and available gaps). Matched Motion packets supply world X/Y/Z coordinates; latest Session packet supplies track length, pit limit and session type. Missing fields are blank, never invented. `time_s` is time since the first sample in this part; `session_time_s` is EA's session timestamp.
- `..._part01_events.csv`: observed garage/pit → track and track → pit transitions, time since initial observation, elapsed time from first pit/garage observation to observed exit, and the game-reported pit-lane/stop timers if captured. **Garage-to-exit includes wait/garage time and is not the same as pit-lane duration.** The first observed active pit-lane timer may be partial if capture started late. Offline pause/wall-clock timing is *not* available from this logic.
- `..._part01_ontrack_stint01.csv` and matching `_speed_vs_time.png`: only samples confirmed on track by **Lap Data**, with the graph time axis reset to 0 at the observed exit. Later pit stops close that stint and begin a new one; plotting runs only **after** capture ends so it cannot block UDP reception. A run that starts on track cannot retrospectively measure an earlier pit departure.
- A flashback or session change starts another `partNN` series; old recordings are not overwritten.

## Gap definitions — when EA provides them

- `race_position`: your live position (P1, P2, ...); `grid_position`: your **starting grid slot** (pole means starting P1, *not* that your live position is P1).
- `gap_ahead_s`: *your car's* reported gap to the car immediately in front, if same-lap and active.
- `gap_behind_s`: the immediately following car's reported gap to *its* car in front (you), if same-lap and active.
- `gap_to_leader_s`: your reported gap to the leader where the leader is on the same lap; zero for P1.
- `car_ahead_index` / `car_behind_index` are packet array indices, **not driver names**. A separate Participants-packet decoder will be needed to label drivers and teams. In qualifying, position can be an order/classification value, not a race result.

If the game doesn't send valid rival data or you're a lap apart, those gaps are **blank**, not zero. EA's reported deltas can differ from circuit distance (metres). No predictions, tyre strategy or automatic passing recommendations are made.

For a *completed enriched on-track stint*, an optional retrospective trend report compares same-position, same-lap reported gaps over the last 5 seconds:

```bash
python3 gap_report.py recordings/YOUR_ACTUAL_ONTRACK_STINT.csv --window 5
```

## Trim the historical CSV you already recorded

Copy that source CSV into this folder or pass its existing absolute path:

```bash
python3 trim_pit_csv.py recordings/f1_2026_20260919_121150_060479_part01.csv
```

For your specific historical sample, the conservative **80 km/h-plateau heuristic** marks its first sustained speed above 84 km/h at **33.966713 s**. The script records `observed_pre_track_elapsed_s` and saves `_ontrack_estimated.csv`, `_ontrack_estimated_speed_vs_time.png`, and `_pit_estimate.json`. It does **not** claim 33.966713 seconds of *pit lane time*, nor does the speed trace prove a pit exit. To override the approximate time after reviewing your graph:

```bash
python3 trim_pit_csv.py recordings/f1_2026_20260919_121150_060479_part01.csv --start-time 34
```

## Local tests

```bash
python3 -m unittest -v test_f1_25_udp.py test_f1_25_udp_v2.py
```

Tests use synthetic 2026 packets, local UDP sockets, and the uploaded historic CSV. They check byte offsets, pit state transitions, gaps, motion data, original-data preservation, on-track time reset, and graphs. **They are not a substitute for a fresh live PS5 session.** If your PS5 displays errors or no enriched laps, send the Terminal output and an `_all.csv` plus `_events.csv` for inspection; no packet captures containing unrelated private data are needed.

EA's published specification: https://forums.ea.com/blog/f1-games-game-info-hub-en/ea-sports%E2%84%A2-f1%C2%AE25-2026-season-pack-udp-specification/12187347
