import csv
import json
from datetime import datetime, timezone
from pathlib import Path


SESSION_FILES = {
    "telemetry": "telemetry.csv",
    "position": "position.csv",
    "lap_summary": "lap_summary.csv",
    "segments": "segments.csv",
    "braking_events": "braking_events.csv",
    "flashback_events": "flashback_events.csv",
    "car_setup": "car_setup.csv",
}

JOIN_KEYS = {
    "car_state": ["session_uid", "vehicle_index"],
    "lap": ["session_uid", "vehicle_index", "lap_number"],
    "segment": ["session_uid", "vehicle_index", "segment_id"],
}


def write_session_manifest(folder: Path) -> Path:
    files = {}
    for name, filename in SESSION_FILES.items():
        path = folder / filename
        with path.open(newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            columns = reader.fieldnames
            if columns is None:
                raise ValueError(f"CSV file has no header: {path}")
            row_count = sum(1 for _ in reader)
        files[name] = {
            "path": filename,
            "row_count": row_count,
            "columns": columns,
        }

    manifest_path = folder / "session_manifest.json"
    manifest = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "join_keys": JOIN_KEYS,
        "files": files,
    }
    with manifest_path.open("w", encoding="utf-8") as manifest_file:
        json.dump(manifest, manifest_file, indent=2)
        manifest_file.write("\n")
    return manifest_path
