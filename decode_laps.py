import argparse
import csv
import struct
from pathlib import Path

from lap_tracking import LapTracker
from telemetry_common import (
    HEADER,
    decode_damage,
    decode_flashback,
    decode_lap,
    decode_motion,
    decode_status,
    decode_telemetry,
    packet_meta,
    valid_2026_packet,
)

LAP_PACKET_ID = 2
EVENT_PACKET_ID = 3
MOTION_PACKET_ID = 0
TELEMETRY_PACKET_ID = 6
STATUS_PACKET_ID = 7
DAMAGE_PACKET_ID = 10
WHEELS = ("rl", "rr", "fl", "fr")

SAMPLE_FIELDS = [
    "received_ns", "udp_format", "session_uid", "session_time_s",
    "frame_id", "overall_frame_id", "vehicle_index", "lap_number",
    "segment_id", "segment_sample_index", "lap_distance_m", "total_distance_m",
    "current_lap_time_s", "last_lap_time_s", "position", "pit_status",
    "sector", "lap_invalid", "driver_status", "speed_kmh", "throttle_pct",
    "steering", "brake_pct", "clutch_pct", "gear", "rpm", "drs",
    "rev_lights_pct", "engine_temp_c", "lap_frame_id", "world_x_m", "world_y_m", "world_z_m",
    "velocity_x_mps", "velocity_y_mps", "velocity_z_mps", "yaw_rad",
    "pitch_rad", "roll_rad", "lap_received_ns", "motion_frame_id",
    "motion_received_ns", "telemetry_frame_id", "telemetry_received_ns",
    "status_frame_id", "status_received_ns", "damage_frame_id", "damage_received_ns",
    "tyre_compound_actual", "tyre_compound_visual", "tyre_age_laps",
    "lap_continuous", "segment_valid", "training_eligible", "exclusion_reason",
]
for wheel in WHEELS:
    SAMPLE_FIELDS.extend([
        f"tyre_surface_temp_{wheel}_c", f"tyre_inner_temp_{wheel}_c",
        f"tyre_pressure_{wheel}_psi", f"brake_temp_{wheel}_c",
        f"tyre_wear_{wheel}_pct", f"tyre_damage_{wheel}_pct",
        f"tyre_blistering_{wheel}_pct",
    ])
for wheel in WHEELS:
    SAMPLE_FIELDS.append(f"surface_type_{wheel}")

LAP_SUMMARY_FIELDS = [
    "session_uid", "vehicle_index", "lap_number", "lap_time_s", "valid",
    "complete", "continuous", "training_eligible", "segment_count",
    "exclusion_reason",
]
SEGMENT_FIELDS = [
    "session_uid", "vehicle_index", "lap_number", "segment_id",
    "start_frame_id", "end_frame_id", "start_session_time_s",
    "end_session_time_s", "start_distance_m", "end_distance_m",
    "segment_duration_s", "coverage_distance_m", "sample_count",
    "segment_start_reason", "segment_end_reason", "segment_valid",
    "training_eligible",
]
FLASHBACK_FIELDS = [
    "session_uid", "vehicle_index", "source_frame_id", "source_session_time_s",
    "target_frame_id", "target_session_time_s", "lap_number",
]
BRAKING_FIELDS = [
    "session_uid", "vehicle_index", "lap_number", "segment_id", "frame_id",
    "overall_frame_id", "session_time_s", "lap_distance_m", "position",
    "world_x_m", "world_y_m", "world_z_m", "speed_kmh", "brake_pct",
    "throttle_pct", "gear", "rpm",
]


def latest_bin(folder):
    recordings = sorted(folder.glob("*.bin"))
    if not recordings:
        raise FileNotFoundError(f"No .bin recordings found in {folder}")
    return recordings[-1]


def telemetry_csv_for(binary_path):
    return binary_path.with_suffix(".csv")


def load_telemetry(csv_path):
    telemetry = {}
    with csv_path.open(newline="") as file:
        for row in csv.DictReader(file):
            key = (int(row["session_uid"]), int(row["frame_id"]))
            telemetry[key] = {
                field: row.get(field, "") for field in (
                    "speed_kmh", "brake_pct", "throttle_pct", "steering",
                    "clutch_pct", "gear", "rpm", "drs", "rev_lights_pct",
                    "engine_temp_c",
                )
            }
            telemetry[key]["telemetry_received_ns"] = row.get("received_ns", "")
            telemetry[key]["telemetry_frame_id"] = row.get("frame_id", "")
    return telemetry


def read_recording(binary_path, telemetry=None, summaries=None, segments=None, flashback_events=None):
    telemetry = telemetry or {}
    rows = []
    latest_lap = {}
    latest_motion = {}
    latest_status = {}
    latest_damage = {}
    latest_telemetry = {}
    trackers = {}

    with binary_path.open("rb") as file:
        while True:
            prefix = file.read(12)
            if not prefix:
                break
            if len(prefix) != 12:
                raise ValueError("Recording ended inside a record header")
            received_ns, packet_length = struct.unpack("<QI", prefix)
            packet = file.read(packet_length)
            if len(packet) != packet_length:
                raise ValueError("Recording ended inside a packet")
            if len(packet) < HEADER.size:
                continue

            meta = packet_meta(packet)
            if meta is None or meta.packet_format != 2026 or meta.packet_version != 1:
                continue
            if meta.player_index >= 24:
                continue

            key = (meta.session_uid, meta.player_index)
            if meta.packet_id == EVENT_PACKET_ID and valid_2026_packet(packet, EVENT_PACKET_ID):
                event = decode_flashback(packet)
                if event["event_code"] == "FLBK":
                    tracker = trackers.setdefault(key, LapTracker(*key))
                    tracker.process_event(
                        event,
                        source_frame_id=meta.frame_id,
                        source_session_time_s=meta.session_time_s,
                    )
                continue
            if meta.packet_id == MOTION_PACKET_ID and valid_2026_packet(packet, MOTION_PACKET_ID):
                latest_motion[key] = {
                    **decode_motion(packet, meta.player_index),
                    "motion_received_ns": received_ns,
                    "motion_frame_id": meta.frame_id,
                }
                continue
            if meta.packet_id == STATUS_PACKET_ID and valid_2026_packet(packet, STATUS_PACKET_ID):
                latest_status[key] = {
                    **decode_status(packet, meta.player_index),
                    "status_received_ns": received_ns,
                    "status_frame_id": meta.frame_id,
                }
                continue
            if meta.packet_id == DAMAGE_PACKET_ID and valid_2026_packet(packet, DAMAGE_PACKET_ID):
                latest_damage[key] = {
                    **decode_damage(packet, meta.player_index),
                    "damage_received_ns": received_ns,
                    "damage_frame_id": meta.frame_id,
                }
                continue
            if meta.packet_id == TELEMETRY_PACKET_ID and valid_2026_packet(packet, TELEMETRY_PACKET_ID):
                latest_telemetry[key] = {
                    **decode_telemetry(packet, meta.player_index),
                    "telemetry_received_ns": received_ns,
                    "telemetry_frame_id": meta.frame_id,
                }
                lap = latest_lap.get(key)
                if lap is None:
                    continue
                car = telemetry.get((meta.session_uid, meta.frame_id), {})
                sample = {
                    "received_ns": received_ns,
                    "udp_format": meta.packet_format,
                    "session_uid": meta.session_uid,
                    "session_time_s": round(meta.session_time_s, 6),
                    "frame_id": meta.frame_id,
                    "overall_frame_id": meta.overall_frame_id,
                    "vehicle_index": meta.player_index,
                    **lap,
                    **car,
                    **latest_motion.get(key, {}),
                    **latest_status.get(key, {}),
                    **latest_damage.get(key, {}),
                    **latest_telemetry.get(key, {}),
                }
                tracker = trackers.setdefault(key, LapTracker(*key))
                sample, _ = tracker.process_sample(sample)
                if sample is not None:
                    rows.append(sample)
                continue
            if meta.packet_id != LAP_PACKET_ID or not valid_2026_packet(packet, LAP_PACKET_ID):
                continue

            lap = decode_lap(packet, meta.player_index)
            latest_lap[key] = {
                **lap,
                "lap_received_ns": received_ns,
                "lap_frame_id": meta.frame_id,
            }

    for tracker in trackers.values():
        tracker.finish()
        if summaries is not None:
            summaries.extend(tracker.summaries)
        if segments is not None:
            segments.extend(tracker.segments)
        if flashback_events is not None:
            flashback_events.extend(tracker.flashback_events)
    return rows


def _write_rows(path, fields, rows):
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_laps(path, laps):
    _write_rows(path, SAMPLE_FIELDS, laps)


def write_lap_summaries(path, summaries):
    _write_rows(path, LAP_SUMMARY_FIELDS, summaries)


def write_segments(path, segments):
    _write_rows(path, SEGMENT_FIELDS, segments)


def write_flashback_events(path, events):
    _write_rows(path, FLASHBACK_FIELDS, events)


def find_braking_events(laps, threshold_pct):
    events = []
    previous_brake = 0.0
    previous_key = None
    for lap in laps:
        brake = lap.get("brake_pct")
        if brake in (None, ""):
            previous_brake = 0.0
            previous_key = None
            continue
        key = (
            lap.get("session_uid"), lap.get("vehicle_index"),
            lap.get("lap_number"), lap.get("segment_id"),
        )
        if key != previous_key:
            previous_brake = 0.0
        if float(brake) >= threshold_pct and previous_brake < threshold_pct:
            events.append({field: lap.get(field, "") for field in BRAKING_FIELDS})
        previous_brake = float(brake)
        previous_key = key
    return events


def write_braking_events(path, events):
    _write_rows(path, BRAKING_FIELDS, events)


def _is_true(value):
    return value is True or str(value).strip().lower() in {"1", "true"}


def _key_value(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def filter_training_rows(rows, segments, summaries):
    eligible_laps = {
        tuple(_key_value(summary.get(field)) for field in (
            "session_uid", "vehicle_index", "lap_number",
        ))
        for summary in summaries
        if _is_true(summary.get("valid"))
        and _is_true(summary.get("complete"))
        and _is_true(summary.get("continuous"))
        and _is_true(summary.get("training_eligible"))
    }
    eligible_segments = {
        tuple(_key_value(segment.get(field)) for field in (
            "session_uid", "vehicle_index", "lap_number", "segment_id",
        ))
        for segment in segments
        if _is_true(segment.get("segment_valid"))
        and _is_true(segment.get("training_eligible"))
        and tuple(_key_value(segment.get(field)) for field in (
            "session_uid", "vehicle_index", "lap_number",
        )) in eligible_laps
    }
    return [
        row for row in rows
        if tuple(_key_value(row.get(field)) for field in (
            "session_uid", "vehicle_index", "lap_number",
        )) in eligible_laps
        and tuple(_key_value(row.get(field)) for field in (
            "session_uid", "vehicle_index", "lap_number", "segment_id",
        )) in eligible_segments
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Decode 2026 telemetry into continuous segments and clean laps."
    )
    parser.add_argument("recording", nargs="?", type=Path)
    parser.add_argument("--brake-threshold", type=float, default=5.0)
    args = parser.parse_args()

    binary_path = args.recording or latest_bin(Path("sessions"))
    csv_path = telemetry_csv_for(binary_path)
    telemetry = load_telemetry(csv_path) if csv_path.exists() else {}
    summaries = []
    segments = []
    flashback_events = []
    laps = read_recording(binary_path, telemetry, summaries, segments, flashback_events)

    laps_path = binary_path.with_name("laps.csv")
    training_laps_path = binary_path.with_name("training_laps.csv")
    summary_path = binary_path.with_name("lap_summary.csv")
    segments_path = binary_path.with_name("segments.csv")
    events_path = binary_path.with_name("braking_events.csv")
    flashback_path = binary_path.with_name("flashback_events.csv")
    write_laps(laps_path, laps)
    training_laps = filter_training_rows(laps, segments, summaries)
    write_laps(training_laps_path, training_laps)
    write_lap_summaries(summary_path, summaries)
    write_segments(segments_path, segments)
    write_flashback_events(flashback_path, flashback_events)
    events = find_braking_events(laps, args.brake_threshold)
    write_braking_events(events_path, events)

    matched = sum(lap.get("speed_kmh") not in (None, "") for lap in laps)
    print(f"Recording: {binary_path}")
    print(f"Telemetry samples: {len(laps)} ({matched} with speed)")
    print(f"Completed clean laps: {sum(bool(row['training_eligible']) for row in summaries)}")
    print(f"Training-eligible samples: {len(training_laps)}")
    print(f"Segments: {len(segments)}")
    print(f"Flashback diagnostics: {len(flashback_events)}")
    for path in (
        laps_path, training_laps_path, summary_path, segments_path,
        events_path, flashback_path,
    ):
        print(f"Wrote: {path}")
    print(f"Braking events (brake >= {args.brake_threshold:.1f}%): {len(events)}")


if __name__ == "__main__":
    main()
