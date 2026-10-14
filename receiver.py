import csv
import socket
import struct
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from decode_laps import (
    BRAKING_FIELDS,
    FLASHBACK_FIELDS,
    LAP_SUMMARY_FIELDS,
    SAMPLE_FIELDS,
    SEGMENT_FIELDS,
)
from lap_tracking import LapTracker
from session_manifest import write_session_manifest
from telemetry_common import (
    CAR_SETUP_FIELDS,
    FramePacketJoiner,
    decode_car_setup,
    decode_damage,
    decode_flashback,
    decode_legacy_telemetry,
    decode_lap,
    decode_motion,
    decode_session,
    decode_status,
    decode_telemetry,
    frame_join_metadata,
    latest_with_frame_age,
    mark_sample_completeness,
    packet_meta,
    valid_packet,
)

PORT = 20777
now = datetime.now()
recording_date = now.strftime("%m-%d-%Y")
recording_id = now.strftime("%H-%M-%S")
SAVE_FOLDER = Path("sessions") / recording_date / recording_id
SAVE_FOLDER.mkdir(parents=True, exist_ok=True)

raw_path = SAVE_FOLDER / "raw.bin"
csv_path = SAVE_FOLDER / "telemetry.csv"
position_path = SAVE_FOLDER / "position.csv"
lap_summary_path = SAVE_FOLDER / "lap_summary.csv"
segments_path = SAVE_FOLDER / "segments.csv"
braking_path = SAVE_FOLDER / "braking_events.csv"
flashback_events_path = SAVE_FOLDER / "flashback_events.csv"
car_setup_path = SAVE_FOLDER / "car_setup.csv"

FORMATS = {2025: (22, 60, 1352), 2026: (24, 59, 1448)}
PACKET_NAMES = {
    0: "Motion", 1: "Session", 2: "Lap Data", 3: "Event",
    4: "Participants", 5: "Car Setups", 6: "Car Telemetry",
    7: "Car Status", 8: "Final Classification", 9: "Lobby Info",
    10: "Car Damage", 11: "Session History", 12: "Tyre Sets",
    13: "Motion Ex", 14: "Time Trial", 15: "Lap Positions",
    16: "Car Telemetry 2",
}
POSITION_FIELDS = [
    "received_ns", "session_uid", "frame_id", "overall_frame_id",
    "vehicle_index", "lap_number", "lap_distance_m", "position",
    "world_x_m", "world_y_m", "world_z_m", "velocity_x_mps",
    "velocity_y_mps", "velocity_z_mps", "yaw_rad", "pitch_rad", "roll_rad",
]


def blank_values() -> dict[str, object]:
    return {field: "" for field in SAMPLE_FIELDS}


def build_telemetry_row(
    received_ns, meta, lap, status, damage, decoded_telemetry, motion=None,
    session=None, setup=None,
):
    values = blank_values()
    values.update({
        "received_ns": received_ns,
        "udp_format": meta.packet_format,
        "session_uid": meta.session_uid,
        "session_time_s": round(meta.session_time_s, 6),
        "frame_id": meta.frame_id,
        "overall_frame_id": meta.overall_frame_id,
        "vehicle_index": meta.player_index,
        **lap,
        **(motion or {}),
        **status,
        **decoded_telemetry,
        **latest_with_frame_age(damage or {}, "damage", meta.overall_frame_id),
        **latest_with_frame_age(setup or {}, "setup", meta.overall_frame_id),
        **latest_with_frame_age(session or {}, "session", meta.overall_frame_id),
    })
    values["received_ns"] = received_ns
    values["lap_received_ns"] = lap.get("lap_received_ns", received_ns)
    return mark_sample_completeness(values, {
        "lap": lap,
        "motion": motion or {},
        "status": status,
        "telemetry": decoded_telemetry,
    })


def build_braking_event(values, previous_brake, previous_key, threshold_pct=5.0):
    key = (
        values.get("session_uid"), values.get("vehicle_index"),
        values.get("lap_number"), values.get("segment_id"),
    )
    if key != previous_key:
        previous_brake = 0.0
    try:
        brake = float(values.get("brake_pct", 0) or 0)
    except (TypeError, ValueError):
        return None, 0.0, key
    event = None
    if brake >= threshold_pct and previous_brake < threshold_pct:
        event = {field: values.get(field, "") for field in BRAKING_FIELDS}
    return event, brake, key


def _write_live_rows(file, fields, rows):
    file.seek(0)
    file.truncate()
    writer = csv.DictWriter(file, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fields})
    file.flush()


def write_live_summaries(file, trackers):
    _write_live_rows(
        file, LAP_SUMMARY_FIELDS,
        [summary for tracker in trackers.values() for summary in tracker.summaries],
    )


def write_live_segments(file, trackers):
    _write_live_rows(
        file, SEGMENT_FIELDS,
        [segment for tracker in trackers.values() for segment in tracker.segments],
    )


def write_live_flashback_events(file, trackers):
    _write_live_rows(
        file, FLASHBACK_FIELDS,
        [event for tracker in trackers.values() for event in tracker.flashback_events],
    )


def main():
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("0.0.0.0", PORT))
    receiver.settimeout(5)
    trackers = {}
    latest_damage = {}
    latest_setup = {}
    latest_session = {}
    frame_joiner = FramePacketJoiner()
    previous_brake = 0.0
    previous_brake_key = None
    sample_count = 0
    packet_counts = Counter()
    packet_versions = Counter()
    packet_sizes = Counter()

    print(f"Listening for F1 telemetry on port {PORT}")
    print("Supported formats: 2025 and 2026")
    print("Press Ctrl+C to stop recording.")

    try:
        with (
            raw_path.open("wb") as raw_file,
            csv_path.open("w+", newline="") as telemetry_file,
            position_path.open("w", newline="") as position_file,
            lap_summary_path.open("w+", newline="") as lap_file,
            segments_path.open("w+", newline="") as segments_file,
            braking_path.open("w", newline="") as braking_file,
            flashback_events_path.open("w+", newline="") as flashback_file,
            car_setup_path.open("w", newline="") as setup_file,
        ):
            telemetry_writer = csv.DictWriter(telemetry_file, SAMPLE_FIELDS)
            position_writer = csv.DictWriter(position_file, POSITION_FIELDS)
            braking_writer = csv.DictWriter(braking_file, BRAKING_FIELDS)
            setup_writer = csv.DictWriter(setup_file, CAR_SETUP_FIELDS)
            telemetry_writer.writeheader()
            position_writer.writeheader()
            braking_writer.writeheader()
            setup_writer.writeheader()
            _write_live_rows(lap_file, LAP_SUMMARY_FIELDS, [])
            _write_live_rows(segments_file, SEGMENT_FIELDS, [])
            _write_live_rows(flashback_file, FLASHBACK_FIELDS, [])

            def emit_joined_sample(joined):
                nonlocal previous_brake, previous_brake_key, sample_count
                metadata = joined["_metadata"]
                meta = SimpleNamespace(
                    packet_format=metadata["packet_format"],
                    player_index=metadata["vehicle_index"],
                    session_uid=metadata["session_uid"],
                    session_time_s=metadata["session_time_s"],
                    frame_id=metadata["frame_id"],
                    overall_frame_id=metadata["overall_frame_id"],
                )
                received_ns = metadata["received_ns"]
                key = (meta.session_uid, meta.player_index)
                tracker = trackers.setdefault(key, LapTracker(*key))
                values = build_telemetry_row(
                    received_ns,
                    meta,
                    joined.get("lap", {}),
                    joined.get("status", {}),
                    metadata["damage"],
                    joined.get("telemetry", {}),
                    joined.get("motion", {}),
                    metadata["session"],
                    metadata["setup"],
                )
                if not values["sample_complete"]:
                    telemetry_writer.writerow({
                        field: values.get(field, "") for field in SAMPLE_FIELDS
                    })
                    position_writer.writerow({
                        field: values.get(field, "") for field in POSITION_FIELDS
                    })
                    sample_count += 1
                    return
                values, completed = tracker.process_sample(values)
                if values is None:
                    return
                telemetry_writer.writerow({
                    field: values.get(field, "") for field in SAMPLE_FIELDS
                })
                position_writer.writerow({
                    field: values.get(field, "") for field in POSITION_FIELDS
                })
                braking_event, previous_brake, previous_brake_key = build_braking_event(
                    values, previous_brake, previous_brake_key
                )
                if braking_event is not None:
                    braking_writer.writerow(braking_event)
                sample_count += 1
                write_live_summaries(lap_file, trackers)
                write_live_segments(segments_file, trackers)
                if sample_count % 60 == 0:
                    telemetry_file.flush()
                    position_file.flush()
                    lap_file.flush()
                    segments_file.flush()
                    braking_file.flush()
                    flashback_file.flush()
                    setup_file.flush()

            def add_frame_packet(meta, received_ns, stream, values):
                key = (meta.session_uid, meta.player_index)
                metadata = frame_join_metadata(
                    meta,
                    received_ns,
                    latest_damage.get(key),
                    latest_setup.get(key),
                    latest_session.get(meta.session_uid),
                )
                joined = frame_joiner.add(
                    meta.session_uid, meta.player_index, meta.frame_id,
                    stream, values, metadata,
                )
                if joined is not None:
                    emit_joined_sample(joined)
                for _, _, expired in frame_joiner.drain_expired():
                    emit_joined_sample(expired)

            try:
                while True:
                    try:
                        packet, _ = receiver.recvfrom(4096)
                    except socket.timeout:
                        print("Waiting for telemetry...")
                        continue

                    received_ns = time.time_ns()
                    raw_file.write(struct.pack("<QI", received_ns, len(packet)))
                    raw_file.write(packet)
                    meta = packet_meta(packet)
                    if meta is None:
                        continue
                    packet_counts[(meta.packet_format, meta.packet_id)] += 1
                    packet_versions[meta.packet_version] += 1
                    packet_sizes[len(packet)] += 1
                    if meta.packet_format not in FORMATS or meta.packet_version != 1:
                        continue
                    car_count, _, _ = FORMATS[meta.packet_format]
                    if meta.packet_id != 1 and not 0 <= meta.player_index < car_count:
                        continue

                    if meta.packet_format == 2025 and valid_packet(packet, 2025, 6):
                        values = blank_values()
                        values.update({
                            "received_ns": received_ns,
                            "udp_format": meta.packet_format,
                            "session_uid": meta.session_uid,
                            "session_time_s": round(meta.session_time_s, 6),
                            "frame_id": meta.frame_id,
                            "overall_frame_id": meta.overall_frame_id,
                            "vehicle_index": meta.player_index,
                            **decode_legacy_telemetry(packet, meta.player_index),
                            "training_eligible": False,
                            "segment_valid": False,
                        })
                        telemetry_writer.writerow(values)
                        sample_count += 1
                        continue
                    if meta.packet_format != 2026 or not valid_packet(packet, 2026, meta.packet_id):
                        continue

                    if meta.packet_id == 1:
                        latest_session[meta.session_uid] = {
                            **decode_session(packet),
                            "session_frame_id": meta.frame_id,
                            "session_received_ns": received_ns,
                            "_overall_frame_id": meta.overall_frame_id,
                        }
                        continue

                    key = (meta.session_uid, meta.player_index)
                    tracker = trackers.setdefault(key, LapTracker(*key))
                    if meta.packet_id == 3:
                        event = decode_flashback(packet)
                        if event["event_code"] == "FLBK":
                            tracker.process_event(
                                event,
                                source_frame_id=meta.frame_id,
                                source_session_time_s=meta.session_time_s,
                            )
                            write_live_segments(segments_file, trackers)
                            write_live_flashback_events(flashback_file, trackers)
                    elif meta.packet_id == 0:
                        motion = {
                            **decode_motion(packet, meta.player_index),
                            "motion_received_ns": received_ns,
                            "motion_frame_id": meta.frame_id,
                        }
                        add_frame_packet(meta, received_ns, "motion", motion)
                    elif meta.packet_id == 2:
                        lap = {
                            **decode_lap(packet, meta.player_index),
                            "lap_received_ns": received_ns,
                            "lap_frame_id": meta.frame_id,
                        }
                        add_frame_packet(meta, received_ns, "lap", lap)
                    elif meta.packet_id == 5:
                        decoded_setup = decode_car_setup(packet, meta.player_index)
                        setup = {
                            **decoded_setup,
                            "setup_frame_id": meta.frame_id,
                            "setup_received_ns": received_ns,
                            "_overall_frame_id": meta.overall_frame_id,
                        }
                        latest_setup[key] = setup
                        setup_writer.writerow({
                            "received_ns": received_ns,
                            "session_uid": meta.session_uid,
                            "session_time_s": round(meta.session_time_s, 6),
                            "frame_id": meta.frame_id,
                            "overall_frame_id": meta.overall_frame_id,
                            "vehicle_index": meta.player_index,
                            **decoded_setup,
                        })
                    elif meta.packet_id == 6:
                        telemetry = {
                            **decode_telemetry(packet, meta.player_index),
                            "telemetry_received_ns": received_ns,
                            "telemetry_frame_id": meta.frame_id,
                        }
                        add_frame_packet(meta, received_ns, "telemetry", telemetry)
                    elif meta.packet_id == 7:
                        status = {
                            **decode_status(packet, meta.player_index),
                            "status_received_ns": received_ns,
                            "status_frame_id": meta.frame_id,
                        }
                        add_frame_packet(meta, received_ns, "status", status)
                    elif meta.packet_id == 10:
                        latest_damage[key] = {
                            **decode_damage(packet, meta.player_index),
                            "damage_received_ns": received_ns,
                            "damage_frame_id": meta.frame_id,
                            "_overall_frame_id": meta.overall_frame_id,
                        }
            except KeyboardInterrupt:
                print("\nRecording stopped.")
            finally:
                for _, _, pending in frame_joiner.flush():
                    emit_joined_sample(pending)
                for tracker in trackers.values():
                    tracker.finish()
                write_live_summaries(lap_file, trackers)
                write_live_segments(segments_file, trackers)
                write_live_flashback_events(flashback_file, trackers)
                raw_file.flush()
    finally:
        receiver.close()
        print(f"Raw recording: {raw_path}")
        print(f"Telemetry CSV: {csv_path}")
        print(f"Position CSV: {position_path}")
        print(f"Lap summary CSV: {lap_summary_path}")
        print(f"Segments CSV: {segments_path}")
        print(f"Braking events CSV: {braking_path}")
        print(f"Flashback events CSV: {flashback_events_path}")
        print(f"Car setup CSV: {car_setup_path}")
        manifest_path = write_session_manifest(SAVE_FOLDER)
        print(f"Session manifest: {manifest_path}")
        print(f"Telemetry samples: {sample_count}")
        print("Observed packets:")
        for (packet_format, packet_id), packet_count in sorted(packet_counts.items()):
            print(f"  format={packet_format}, id={packet_id} ({PACKET_NAMES.get(packet_id, 'Unknown')}), count={packet_count}")
        print(f"Packet versions: {dict(sorted(packet_versions.items()))}")
        print(f"Packet sizes: {dict(sorted(packet_sizes.items()))}")


if __name__ == "__main__":
    main()
