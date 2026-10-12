"""Non-destructively trim an old speed-only F1 CSV; estimates are NOT pit telemetry.

python3 trim_pit_csv.py recordings/your_file.csv --start-time 34
python3 trim_pit_csv.py recordings/your_file.csv  # conservative 80 km/h plateau heuristic
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import telemetry_graph


def load_rows(path: Path) -> tuple[list[dict], list[float]]:
    # Reuse existing strict validator rather than silently cleaning corrupt input.
    times, speeds = telemetry_graph.load_telemetry(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError("CSV is missing its header")
        rows = list(reader)
    if len(rows) != len(times):
        raise ValueError("CSV changed while being read")
    return rows, times


def suggest_exit(rows: list[dict], times: list[float],
                 limit_low: float = 75, limit_high: float = 82,
                 exit_speed: float = 84, plateau_s: float = 3,
                 sustained_s: float = 0.5) -> float:
    """Speed-only proxy: >=3s near ~80, followed by >=0.5s above 84.

    This is not a proof of a pit exit. Does not claim to find a pit on other tracks.
    """
    speeds = [float(r["speed_kmh"]) for r in rows]
    # Locate first contiguous exit after a long near-limit plateau.
    for i, (t, speed) in enumerate(zip(times, speeds)):
        if speed <= exit_speed:
            continue
        before = [speeds[j] for j in range(i) if t - plateau_s <= times[j] < t]
        if len(before) < 20 or sum(limit_low <= v <= limit_high for v in before) < 0.8 * len(before):
            continue
        after = [speeds[j] for j in range(i, len(rows)) if t <= times[j] <= t + sustained_s]
        if not after or times[-1] < t + sustained_s or min(after) <= exit_speed:
            continue
        return t
    raise ValueError("No convincing 80 km/h plateau/exit found. Use --start-time manually; speed alone cannot confirm pit status.")


def trim(path: Path, start_time: float | None = None) -> tuple[Path, Path, Path]:
    rows, times = load_rows(path)
    if start_time is not None and not math.isfinite(start_time):
        raise ValueError("--start-time must be finite")
    method = "manual_from_graph" if start_time is not None else "speed_plateau_estimate_not_confirmed"
    if start_time is None:
        start_time = suggest_exit(rows, times)
    if start_time < times[0] or start_time >= times[-1]:
        raise ValueError("--start-time must be within the CSV timeline, before the final sample")
    selected = [(row, t) for row, t in zip(rows, times) if t >= start_time]
    if len(selected) < 2:
        raise ValueError("At least two samples after the start time are required")
    output = path.with_name(path.stem + "_ontrack_estimated.csv")
    if output.resolve() == path.resolve():
        raise ValueError("Output would overwrite the source CSV")
    # Original seven fields retained, time_s reset; additional source_time_s for audit.
    cols = list(rows[0].keys()) + ["source_time_s"]
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for row, t in selected:
            new_row = dict(row)
            new_row["time_s"] = f"{t - start_time:.6f}"
            new_row["source_time_s"] = row["time_s"]
            writer.writerow(new_row)
    png = output.with_name(output.stem + "_speed_vs_time.png")
    ts, speeds = telemetry_graph.load_telemetry(output)
    telemetry_graph.save_graph(ts, speeds, png, show=False)
    summary = path.with_name(path.stem + "_pit_estimate.json")
    summary.write_text(json.dumps({
        "source": path.name,
        "method": method,
        "pit_exit_confirmed_by_game": False,
        "recording_start_s": times[0],
        "estimated_on_track_start_s": start_time,
        "observed_pre_track_elapsed_s": round(start_time - times[0], 6),
        "retained_samples": len(selected),
        "excluded_samples": len(rows) - len(selected),
        "retained_duration_s": round(times[-1] - start_time, 6),
        "pit_lane_duration_s": None,
        "paused_duration_s": None,
        "note": "All pre-track time includes any initial garage/wait time. No pit state or rivals were recorded in this CSV. An 80 km/h plateau alone cannot establish the true pit exit."
    }, indent=2) + "\n", encoding="utf-8")
    return output, png, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--start-time", type=float,
                        help="Approximate on-track start on source CSV's time_s axis; otherwise speed heuristic")
    args = parser.parse_args()
    try:
        out, png, summary = trim(args.csv_file, args.start_time)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Cannot trim CSV: {exc}\n")
    print("Saved (original CSV unchanged):", out, png, summary, sep="\n  ")
    print("Important: speed-only data cannot confirm pit exit or actual pit-lane duration.")


if __name__ == "__main__":
    main()
