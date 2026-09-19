"""Describe observed position/gap trends in enriched 2026 CSV (not a forecast).

python3 gap_report.py recordings/NAME_part01_ontrack_stint01.csv --window 5
"""
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path


def gap_summary(path: Path, window: float = 5.0) -> str:
    if window <= 0 or not math.isfinite(window):
        raise ValueError("window must be positive and finite")
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required = {"time_s", "status", "lap_number", "race_position",
                    "gap_ahead_s", "gap_behind_s", "gap_to_leader_s"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("Requires an enriched v2 CSV, not the original seven-column recording")
        rows = list(reader)
    valid = []
    previous_t = -1.0
    for row in rows:
        try:
            t = float(row["time_s"])
            if not math.isfinite(t) or t <= previous_t:
                raise ValueError("time_s must strictly increase")
            previous_t = t
            if row["status"] != "on_track" or not row["race_position"] or not row["lap_number"]:
                continue
            pos = int(row["race_position"])
            lap = int(row["lap_number"])
            if not 1 <= pos <= 24 or lap < 1:
                continue
            gap = row["gap_behind_s"] if pos == 1 else row["gap_ahead_s"]
            if not gap:
                continue
            gap = float(gap)
            if not math.isfinite(gap) or gap <= 0 or gap > 600:
                continue
            valid.append((t, pos, lap, gap))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid numeric CSV field: {exc}") from exc
    if not valid:
        return "No valid same-lap opponent gaps available. Check Restricted telemetry and on-track lap packets."
    end = valid[-1]
    # Only compare the same position and lap: pit stops/position changes invalidate deltas.
    candidates = [r for r in valid if r[1:3] == end[1:3]
                  and end[0] - window <= r[0] <= end[0]]
    if len(candidates) < 2 or end[0] - candidates[0][0] < window * 0.6:
        return (f"Last observed P{end[1]}, lap {end[2]}, gap {end[3]:.3f}s. "
                "Not enough uninterrupted same-position data for a trend.")
    start = candidates[0]
    delta = end[3] - start[3]
    if end[1] == 1:
        meaning = "gap behind opening" if delta > 0.05 else "gap behind closing" if delta < -0.05 else "gap behind stable"
        label = "gap behind"
    else:
        meaning = "closing on car ahead" if delta < -0.05 else "falling back from car ahead" if delta > 0.05 else "gap ahead stable"
        label = "gap ahead"
    return (f"Observed P{end[1]} | lap {end[2]} | {label} {end[3]:.3f}s | "
            f"change {delta:+.3f}s over {end[0]-start[0]:.2f}s ({meaning}). "
            "This is an observed gap trend, not a prediction or a pit strategy recommendation.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--window", type=float, default=5)
    args = parser.parse_args()
    try:
        print(gap_summary(args.csv_file, args.window))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Cannot analyze gaps: {exc}\n")


if __name__ == "__main__":
    main()
