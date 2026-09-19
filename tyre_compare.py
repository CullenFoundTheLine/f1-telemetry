"""Summarise valid, game-reported lap times by fitted tyre compound.

Usage: python3 tyre_compare.py recordings/NAME_laps.csv
       python3 tyre_compare.py recordings/NAME_laps.csv --car-index 21
This reports an OBSERVED comparison, not a causal estimate of tyre performance.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path


def compare(path: Path, car_index: int | None = None) -> list[str]:
    with path.open(newline='', encoding='utf-8-sig') as fh:
        reader = csv.DictReader(fh, strict=True)
        required = {'session_uid', 'timeline', 'car_index', 'is_player', 'lap_number',
                    'lap_time_s', 'valid_lap', 'visual_tyre'}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError('Expected a _laps.csv from f1_25_monitor.py')
        rows = list(reader)

    # Never mix cars or sessions; only use the player unless explicitly selected.
    selected = [r for r in rows if (r['car_index'] == str(car_index) if car_index is not None
                                    else r['is_player'] == '1')]
    if not selected:
        return ['No captured completed laps for the selected car.']
    by_session = defaultdict(lambda: defaultdict(list))
    skipped = 0
    for r in selected:
        if r['valid_lap'] != '1' or not r['visual_tyre']:
            skipped += 1
            continue
        try:
            t = float(r['lap_time_s'])
        except ValueError:
            skipped += 1
            continue
        if not math.isfinite(t) or not 10 <= t <= 1800:
            skipped += 1
            continue
        key = (r['session_uid'], r['timeline'], r['car_index'])
        by_session[key][r['visual_tyre']].append(t)
    lines = []
    for (uid, timeline, car), groups in by_session.items():
        lines.append(f'Session {uid}, timeline {timeline}, car index {car} — valid completed laps')
        for compound, samples in sorted(groups.items()):
            fastest = min(samples)
            median = statistics.median(samples)
            lines.append(f'  {compound:14} {len(samples):2} lap(s) | best {fastest:.3f}s | '
                         f'median {median:.3f}s')
        if len(groups) > 1:
            fastest_by_compound = {compound: min(t) for compound, t in groups.items()}
            base = min(fastest_by_compound.values())
            lines.append('  Observed best-lap differences: ' + ', '.join(
                f'{compound} +{best - base:.3f}s' for compound, best
                in sorted(fastest_by_compound.items())))
        lines.append('  Not tyre-only effects: fuel, weather, track evolution, wear and driver inputs may differ.')
    if not lines:
        lines.append('No valid laps with known tyres yet. Check Lap History / tyre stint packets.')
    if skipped:
        lines.append(f'Excluded {skipped} lap(s) with invalid times or unknown tyre labels.')
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('laps_csv', type=Path)
    parser.add_argument('--car-index', type=int, help='Inspect another car instead of the player')
    args = parser.parse_args()
    try:
        for line in compare(args.laps_csv, args.car_index):
            print(line)
    except (OSError, ValueError, csv.Error) as exc:
        parser.exit(2, f'Cannot compare tyres: {exc}\n')


if __name__ == '__main__':
    main()
