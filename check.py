"""check.py - one command to verify the project after a change or a recording.

Usage (from the f1-telemetry folder):
    python3 check.py                      # tests + replay newest recording
    python3 check.py path/to/raw.bin      # tests + replay that recording
    python3 check.py --commit "message"   # also commit and push, but ONLY if tests pass

It never edits your code. The only things it writes are the derived CSVs that
decode_laps.py always writes next to raw.bin, plus the git commit if you ask.
"""
import csv
import collections
import re
import statistics
import subprocess
import sys
from pathlib import Path

PY = sys.executable
CODE_FILES = [
    "receiver.py", "decode_laps.py", "lap_tracking.py", "packet_pipeline.py",
    "telemetry_common.py", "session_manifest.py", "packet_cadence.py",
    "check.py", "test_telemetry.py",
]


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def step_tests():
    out = run([PY, "-m", "unittest", "test_telemetry"])
    text = out.stdout + out.stderr
    ran = re.search(r"Ran (\d+) tests?", text)
    ok = out.returncode == 0
    print(f"[tests]  {'PASS' if ok else 'FAIL'}  ({ran.group(1) if ran else '?'} tests)")
    if not ok:
        # Show only the failure headlines, not the whole log.
        for line in text.splitlines():
            if line.startswith(("FAIL:", "ERROR:")) or "Error" in line[:40]:
                print("         " + line)
    return ok


def newest_recording():
    found = sorted(Path("sessions").glob("*/*/raw.bin"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def step_replay(raw):
    out = run([PY, "decode_laps.py", str(raw)])
    if out.returncode != 0:
        print("[replay] FAIL\n" + out.stderr[-600:])
        return None
    counts = {}
    for line in out.stdout.splitlines():
        for key in ("Telemetry samples", "Completed clean laps",
                    "Training-eligible samples", "Segments", "Flashback diagnostics"):
            if line.startswith(key):
                counts[key] = line.split(":", 1)[1].strip()
    print(f"[replay] {raw}")
    for k, v in counts.items():
        print(f"         {k}: {v}")
    return raw.parent


def step_laps(folder):
    path = folder / "lap_summary.csv"
    rows = list(csv.DictReader(open(path, newline="")))
    print(f"[laps]   {len(rows)} lap record(s)   (eligible = usable for training)")
    print(f"         {'lap':>3} {'time_s':>8} {'eligible':>8}  {'condition':<9} reason")
    for r in rows:
        print(f"         {r['lap_number']:>3} {r.get('lap_time_s') or '-':>8} "
              f"{r['training_eligible']:>8}  {r.get('condition_class', '-'):<9} "
              f"{r.get('exclusion_reason') or '-'}")
    train = folder / "training_laps.csv"
    if train.exists():
        per_lap = collections.Counter(r["lap_number"] for r in csv.DictReader(open(train, newline="")))
        print(f"[train]  training_laps.csv rows per lap: {dict(per_lap) or 'none'}")


def step_sync(folder):
    cols = ["lap_frame_id", "motion_frame_id", "telemetry_frame_id", "status_frame_id"]
    total = same = 0
    for r in csv.DictReader(open(folder / "laps.csv", newline="")):
        total += 1
        same += all(r.get(c, "") == r["frame_id"] for c in cols)
    pct = same / total * 100 if total else 0
    print(f"[sync]   fast streams on the same frame: {pct:.1f}% of {total} rows")


def step_sanity(folder):
    """Each eligible lap should have about (lap time x sample rate) rows.
    A lap with far more rows has samples from another stretch of track mixed in."""
    train = folder / "training_laps.csv"
    if not train.exists():
        return
    per_lap = collections.Counter(r["lap_number"] for r in csv.DictReader(open(train, newline="")))
    times = {}
    for r in csv.DictReader(open(folder / "lap_summary.csv", newline="")):
        if r["training_eligible"] in ("True", "1") and r.get("lap_time_s"):
            times[r["lap_number"]] = float(r["lap_time_s"])
    ratios = {lap: n / times[lap] for lap, n in per_lap.items() if times.get(lap)}
    if not ratios:
        return
    med = statistics.median(ratios.values())
    bad = [(lap, per_lap[lap], round(times[lap] * med)) for lap, x in ratios.items()
           if abs(x - med) / med > 0.03]
    print(f"[sanity] typical rate {med:.1f} rows per second of lap time")
    if bad:
        for lap, got, want in bad:
            print(f"         SUSPECT lap {lap}: {got} rows, expected about {want} - do not trust it")
    else:
        print("         every eligible lap's row count matches its lap time")


def step_columns(folder):
    """Catch column-name collisions and dead sensor columns in training_laps.csv."""
    train = folder / "training_laps.csv"
    if not train.exists():
        return
    with open(train, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        dups = sorted({c for c, n in collections.Counter(header).items() if n > 1})
        watch = [i for i, c in enumerate(header)
                 if c.startswith(("tyre_pressure_", "tyre_surface_temp_", "tyre_inner_temp_",
                                  "brake_temp_", "speed_kmh", "rpm"))]
        seen = {i: set() for i in watch}
        for row in reader:
            for i in watch:
                if len(seen[i]) < 3:
                    seen[i].add(row[i])
    frozen = sorted(header[i] for i, v in seen.items() if len(v) <= 1)
    problems = []
    if dups:
        problems.append("duplicate column names: " + ", ".join(dups))
    if frozen:
        problems.append("columns that never change (dead or overwritten?): " + ", ".join(frozen))
    print("[columns] " + ("OK - no duplicate names, no frozen sensor columns" if not problems else "PROBLEM"))
    for line in problems:
        print("          " + line)


def step_commit(message, tests_ok):
    if not tests_ok:
        print("[commit] skipped - tests are failing")
        return
    existing = [f for f in CODE_FILES if Path(f).exists()]
    run(["git", "add", "--"] + existing)
    out = run(["git", "commit", "-m", message])
    print("[commit] " + (out.stdout.strip().splitlines() or ["nothing to commit"])[0])
    if out.returncode == 0:
        push = run(["git", "push"])
        print("[push]   " + ("done" if push.returncode == 0 else "FAILED: " + push.stderr.strip()[-200:]))


def main():
    args = sys.argv[1:]
    message = None
    if "--commit" in args:
        i = args.index("--commit")
        message = args[i + 1]
        del args[i:i + 2]
    tests_ok = step_tests()
    raw = Path(args[0]) if args else newest_recording()
    if raw is None or not raw.exists():
        print("[replay] no raw.bin found - skipped")
    else:
        folder = step_replay(raw)
        if folder:
            step_laps(folder)
            step_sanity(folder)
            step_columns(folder)
            step_sync(folder)
    if message:
        step_commit(message, tests_ok)


if __name__ == "__main__":
    main()