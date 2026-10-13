import csv
import io
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from decode_laps import (
    LAP_SUMMARY_FIELDS,
    WHEELS,
    filter_training_rows,
    find_braking_events,
    main as decode_laps_main,
    read_recording,
)
from lap_tracking import DiscontinuityDetector, LapTracker
from packet_pipeline import PipelineState
from receiver import (
    SAMPLE_FIELDS,
    build_braking_event,
    build_telemetry_row,
)
from telemetry_common import (
    CAR_SETUP,
    HEADER,
    PACKET_SIZES_2026,
    decode_car_setup,
    decode_damage,
    decode_lap,
    decode_motion,
    decode_session,
    decode_status,
    decode_telemetry,
    classify_compound,
    classify_weather,
    lap_condition_class,
    packet_meta,
    valid_packet,
)
from unittest.mock import patch


def make_sample(
    *, lap=1, current=0.5, session_time=10.0, distance=100.0, frame=1,
    received_ns=None, session=1, vehicle=0, invalid=0, last=0.0,
    speed=100.0, pit=0, brake=0.0, throttle=0.0, **extra,
):
    if received_ns is None:
        received_ns = frame * 16_000_000
    return {
        "session_uid": session,
        "vehicle_index": vehicle,
        "lap_number": lap,
        "current_lap_time_s": current,
        "last_lap_time_s": last,
        "lap_distance_m": distance,
        "total_distance_m": distance,
        "session_time_s": session_time,
        "frame_id": frame,
        "overall_frame_id": frame,
        "received_ns": received_ns,
        "lap_invalid": invalid,
        "pit_status": pit,
        "speed_kmh": speed,
        "brake_pct": brake,
        "throttle_pct": throttle,
        **extra,
    }


def make_packet(packet_id, *, session=1, frame=1, player=0, session_time=1.0):
    size = PACKET_SIZES_2026[packet_id]
    header = HEADER.pack(
        2026, 26, 0, 0, 1, packet_id, session, session_time,
        frame, frame, player, 255,
    )
    return bytearray(header + bytes(size - HEADER.size))


def lap_packet(*, lap=1, current_ms=500, last_ms=0, distance=100.0, frame=1,
               session=1, player=0, session_time=1.0, invalid=0):
    packet = make_packet(2, session=session, frame=frame, player=player, session_time=session_time)
    struct.pack_into("<II", packet, HEADER.size, last_ms, current_ms)
    float_offset = HEADER.size + struct.calcsize("<IIHBHBHBHB")
    struct.pack_into("<ff", packet, float_offset, distance, distance)
    packet[HEADER.size + 33] = lap
    packet[HEADER.size + 37] = invalid
    return bytes(packet)


def telemetry_packet(*, speed=250, session=1, frame=1, player=0):
    packet = make_packet(6, session=session, frame=frame, player=player)
    offset = HEADER.size + player * struct.calcsize("<HfffBbHBBH4H4B4BB4f4B")
    struct.pack_into("<HfffBbH", packet, offset, speed, 0.75, -0.25, 0.5, 20, 7, 10000)
    packet[offset + 30] = 70
    packet[offset + 31] = 71
    return bytes(packet)


def flashback_packet(*, target_frame=1, target_time=1.0, session=1, frame=100):
    packet = make_packet(3, session=session, frame=frame)
    packet[HEADER.size:HEADER.size + 4] = b"FLBK"
    struct.pack_into("<If", packet, HEADER.size + 4, target_frame, target_time)
    return bytes(packet)


class DiscontinuityTests(unittest.TestCase):
    def test_frame_regression_closes_segment(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(frame=30))
        tracker.process_sample(make_sample(frame=31, current=0.6, session_time=10.016))
        tracker.process_sample(make_sample(frame=10, current=0.7, session_time=10.032))
        self.assertEqual(tracker.segments[0]["segment_end_reason"], "frame_regression")
        current = tracker.segment_tracker.current
        if current is None:
            self.fail("A new segment should be active after frame regression")
        self.assertEqual(current["segment_id"], 2)

    def test_session_time_regression_closes_segment(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(frame=1, session_time=10))
        tracker.process_sample(make_sample(frame=2, current=0.6, session_time=9.8))
        self.assertEqual(tracker.segments[0]["segment_end_reason"], "session_time_regression")

    def test_lap_time_regression_closes_segment(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(frame=1, current=10, session_time=10))
        tracker.process_sample(make_sample(frame=2, current=8, session_time=10.016))
        self.assertEqual(tracker.segments[0]["segment_end_reason"], "lap_time_regression")

    def test_large_same_lap_distance_regression_closes_segment(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(frame=1, distance=500))
        tracker.process_sample(make_sample(frame=2, current=0.6, distance=449))
        self.assertEqual(tracker.segments[0]["segment_end_reason"], "distance_regression")

    def test_small_distance_noise_does_not_split_segment(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(frame=1, distance=100))
        row, _ = tracker.process_sample(make_sample(frame=2, current=0.6, distance=99.9))
        self.assertEqual(row["segment_id"], 1)
        self.assertEqual(tracker.segments, [])

    def test_packet_gap_closes_segment(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(frame=1, session_time=10))
        tracker.process_sample(make_sample(frame=60, current=1.5, session_time=10.8))
        self.assertEqual(tracker.segments[0]["segment_end_reason"], "packet_gap")

    def test_short_packet_timing_variation_is_accepted(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(frame=1, session_time=10))
        row, _ = tracker.process_sample(make_sample(frame=4, current=0.55, session_time=10.05))
        self.assertEqual(row["segment_id"], 1)
        self.assertEqual(tracker.segments, [])

    def test_short_duplicate_packets_are_ignored(self):
        tracker = LapTracker(1, 0)
        original = make_sample(frame=1, received_ns=0)
        tracker.process_sample(original)
        duplicate, completed = tracker.process_sample({**original, "received_ns": 100_000_000})
        self.assertIsNone(duplicate)
        self.assertIsNone(completed)
        current = tracker.segment_tracker.current
        if current is None:
            self.fail("The original segment should still be active")
        self.assertEqual(current["sample_count"], 1)

    def test_prolonged_frozen_state_closes_segment_and_resume_starts_new_one(self):
        tracker = LapTracker(1, 0)
        original = make_sample(frame=1, received_ns=0)
        tracker.process_sample(original)
        for stamp in (100_000_000, 600_000_000):
            self.assertIsNone(tracker.process_sample({**original, "received_ns": stamp})[0])
        frozen, _ = tracker.process_sample({**original, "received_ns": 1_200_000_000})
        self.assertIsNone(frozen)
        self.assertEqual(tracker.segments[0]["segment_end_reason"], "telemetry_freeze")
        resumed, _ = tracker.process_sample(make_sample(
            frame=2, current=0.7, session_time=10.2, distance=101,
            received_ns=1_216_000_000,
        ))
        self.assertEqual(resumed["segment_id"], 2)
        current = tracker.segment_tracker.current
        if current is None:
            self.fail("The resumed segment should be active")
        self.assertEqual(current["segment_start_reason"], "telemetry_resume")

    def test_normal_finish_line_distance_and_time_reset_is_not_discontinuity(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(lap=4, current=0.5, session_time=10, distance=0, frame=1))
        tracker.process_sample(make_sample(lap=4, current=91.9, session_time=10.016, distance=5000, frame=2))
        _, completed = tracker.process_sample(make_sample(
            lap=5, current=0.2, last=92.0, session_time=10.032,
            distance=0, frame=3,
        ))
        self.assertTrue(completed["complete"])
        self.assertTrue(completed["continuous"])
        self.assertEqual(completed["segment_count"], 1)
        self.assertEqual(tracker.segments[0]["segment_end_reason"], "lap_complete")

    def test_normal_lap_number_transition_is_not_a_discontinuity(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(lap=1, frame=1))
        row, _ = tracker.process_sample(make_sample(
            lap=2, current=0.2, session_time=10.016, distance=0, frame=2,
        ))
        self.assertEqual(row["segment_id"], 2)
        self.assertEqual(tracker.segments[0]["segment_end_reason"], "lap_complete")

    def test_negative_lap_distance_near_line_is_preserved(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(distance=-5.0, frame=1))
        row, _ = tracker.process_sample(make_sample(distance=-4.0, frame=2, current=0.6))
        self.assertEqual(row["lap_distance_m"], -4.0)
        self.assertEqual(row["segment_id"], 1)

    def test_discontinuity_tolerances_are_explicit(self):
        detector = DiscontinuityDetector()
        detector.inspect(make_sample(frame=1, session_time=10, current=10, distance=100))
        result = detector.inspect(make_sample(frame=2, session_time=9.98, current=9.98, distance=99.9))
        self.assertEqual(result.end_reason, "")


class LapAndSegmentTests(unittest.TestCase):
    def test_condition_classification_helpers(self):
        self.assertEqual(classify_weather(0), "dry")
        self.assertEqual(classify_weather(2), "dry")
        self.assertEqual(classify_weather(3), "wet")
        self.assertEqual(classify_weather(5), "wet")
        for code in (9, "", None):
            self.assertEqual(classify_weather(code), "unknown")
        self.assertEqual(classify_compound(16), "dry")
        self.assertEqual(classify_compound(22), "dry")
        self.assertEqual(classify_compound(7), "intermediate")
        self.assertEqual(classify_compound(8), "wet")
        for code in (99, "", None):
            self.assertEqual(classify_compound(code), "unknown")
        self.assertEqual(lap_condition_class({"dry"}, {"dry"}), "dry")
        self.assertEqual(lap_condition_class({"unknown"}, {"dry"}), "unknown")

    def test_lap_summary_classifies_stable_dry_conditions(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(
            lap=1, current=0.5, frame=1, weather_code=0,
            tyre_compound_actual=18,
        ))
        _, completed = tracker.process_sample(make_sample(
            lap=2, current=0.2, last=92.0, frame=2,
            session_time=10.016, weather_code=0, tyre_compound_actual=18,
        ))
        self.assertEqual(completed["condition_class"], "dry")
        self.assertFalse(completed["weather_changed_in_lap"])

    def test_lap_summary_detects_weather_change_to_wet(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(
            lap=1, current=0.5, frame=1, weather_code=0,
            tyre_compound_actual=18,
        ))
        tracker.process_sample(make_sample(
            lap=1, current=20.0, frame=2, session_time=10.016,
            weather_code=3, tyre_compound_actual=18,
        ))
        _, completed = tracker.process_sample(make_sample(
            lap=2, current=0.2, last=92.0, frame=3,
            session_time=10.032, weather_code=3, tyre_compound_actual=18,
        ))
        self.assertTrue(completed["weather_changed_in_lap"])
        self.assertEqual(completed["weather_codes_seen"], "0|3")
        self.assertEqual(completed["condition_class"], "wet")

    def test_lap_summary_classifies_intermediate_compound_as_wet(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(
            lap=1, current=0.5, frame=1, weather_code=0,
            tyre_compound_actual=7,
        ))
        _, completed = tracker.process_sample(make_sample(
            lap=2, current=0.2, last=92.0, frame=2,
            session_time=10.016, weather_code=0, tyre_compound_actual=7,
        ))
        self.assertEqual(completed["condition_class"], "wet")

    def test_lap_without_weather_is_unknown_and_summary_fields_are_defined(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(lap=1, current=0.5, frame=1))
        _, completed = tracker.process_sample(make_sample(
            lap=2, current=0.2, last=92.0, frame=2, session_time=10.016,
        ))
        self.assertEqual(completed["condition_class"], "unknown")
        for field in (
            "weather_codes_seen", "weather_changed_in_lap",
            "compound_codes_seen", "condition_class",
        ):
            self.assertIn(field, LAP_SUMMARY_FIELDS)

    def test_uninterrupted_lap_completes_once_with_one_segment(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(lap=1, current=0.5, frame=1))
        tracker.process_sample(make_sample(lap=1, current=91.9, frame=2, session_time=10.016, distance=5000))
        _, completed = tracker.process_sample(make_sample(
            lap=2, current=0.2, last=92.0, frame=3, session_time=10.032, distance=0,
        ))
        self.assertEqual(completed["lap_number"], 1)
        self.assertTrue(completed["valid"])
        self.assertTrue(completed["training_eligible"])
        self.assertEqual(completed["segment_count"], 1)
        self.assertEqual(len(tracker.segments), 1)

    def test_repeated_lap_data_does_not_duplicate_completion(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(lap=1, current=0.5, frame=1))
        _, first = tracker.process_sample(make_sample(lap=2, current=0.2, frame=2, session_time=10.016))
        _, second = tracker.process_sample(make_sample(lap=2, current=0.3, frame=3, session_time=10.032))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(tracker.summaries), 1)

    def test_game_marked_invalid_lap_is_stored_but_not_training_eligible(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(lap=1, current=0.5, frame=1))
        tracker.process_sample(make_sample(lap=1, current=20, invalid=1, frame=2, session_time=10.016))
        _, completed = tracker.process_sample(make_sample(
            lap=2, current=0.2, last=91, frame=3, session_time=10.032,
        ))
        self.assertFalse(completed["valid"])
        self.assertFalse(completed["training_eligible"])
        self.assertEqual(completed["exclusion_reason"], "game_marked_invalid")
        self.assertFalse(tracker.segments[0]["segment_valid"])

    def test_extended_non_pit_stop_invalidates_lap(self):
        tracker = LapTracker(1, 0)
        rows = []
        tracker.process_sample(make_sample(current=0.5, speed=100, frame=1, session_time=10))
        for index in range(1, 8):
            row, _ = tracker.process_sample(make_sample(
                current=0.5 + index * 0.5, session_time=10 + index * 0.5,
                speed=0, frame=index + 1, received_ns=(index + 1) * 500_000_000,
            ))
            if row is not None:
                rows.append(row)
        self.assertTrue(tracker.invalid)
        self.assertEqual(tracker.exclusion_reason, "extended_low_speed")
        tracker.finish()
        self.assertFalse(tracker.segments[0]["segment_valid"])
        self.assertFalse(tracker.segments[0]["training_eligible"])
        self.assertFalse(filter_training_rows(rows, tracker.segments, tracker.summaries))

    def test_pit_stop_does_not_invalidate_lap(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(current=0.5, speed=100, frame=1, session_time=10))
        for index in range(1, 8):
            tracker.process_sample(make_sample(
                current=0.5 + index * 0.5, session_time=10 + index * 0.5,
                speed=0, pit=1, frame=index + 1,
                received_ns=(index + 1) * 500_000_000,
            ))
        self.assertFalse(tracker.invalid)
        tracker.finish()
        self.assertEqual(tracker.exclusion_reason, "")
        self.assertTrue(tracker.segments[0]["segment_valid"])

    def test_final_segment_status_overrides_provisional_row_eligibility(self):
        tracker = LapTracker(1, 0)
        rows = []
        for index in range(8):
            row, _ = tracker.process_sample(make_sample(
                current=0.5 + index * 0.5,
                session_time=10 + index * 0.5,
                speed=100 if index < 2 else 0,
                frame=index + 1,
                received_ns=(index + 1) * 500_000_000,
            ))
            if row is not None:
                rows.append(row)

        self.assertTrue(rows[0]["training_eligible"])
        tracker.finish()
        self.assertFalse(tracker.segments[0]["segment_valid"])
        self.assertFalse(tracker.segments[0]["training_eligible"])
        self.assertEqual(filter_training_rows(rows, tracker.segments, tracker.summaries), [])

    def test_final_training_filter_accepts_csv_boolean_values(self):
        rows = [
            {"session_uid": 1, "vehicle_index": 0, "lap_number": 1, "segment_id": 1},
            {"session_uid": 1, "vehicle_index": 0, "lap_number": 1, "segment_id": 2},
        ]
        segments = [
            {"session_uid": "1", "vehicle_index": "0", "lap_number": "1", "segment_id": "1",
             "segment_valid": "True", "training_eligible": "True"},
            {"session_uid": "1", "vehicle_index": "0", "lap_number": "1", "segment_id": "2",
             "segment_valid": "True", "training_eligible": "False"},
        ]
        summaries = [{
            "session_uid": "1", "vehicle_index": "0", "lap_number": "1",
            "valid": "True", "complete": "True", "continuous": "True",
            "training_eligible": "True",
        }]
        self.assertEqual(filter_training_rows(rows, segments, summaries), [rows[0]])

    def test_incomplete_lap_segment_is_not_training_eligible(self):
        rows = [{
            "session_uid": 1, "vehicle_index": 0,
            "lap_number": 1, "segment_id": 1,
        }]
        segments = [{
            "session_uid": 1, "vehicle_index": 0, "lap_number": 1,
            "segment_id": 1, "segment_valid": True,
            "training_eligible": True,
        }]
        summaries = [{
            "session_uid": 1, "vehicle_index": 0, "lap_number": 1,
            "valid": False, "complete": False, "continuous": True,
            "training_eligible": False,
        }]

        self.assertEqual(filter_training_rows(rows, segments, summaries), [])

    def test_partial_continuous_segment_is_training_eligible(self):
        tracker = LapTracker(1, 0)
        rows = []
        for sample in (
            make_sample(frame=1, distance=100),
            make_sample(frame=2, current=0.6, session_time=10.016, distance=110),
        ):
            row, _ = tracker.process_sample(sample)
            if row is not None:
                rows.append(row)
        tracker.finish()
        self.assertFalse(tracker.summaries[0]["complete"])
        self.assertTrue(tracker.segments[0]["segment_valid"])
        self.assertTrue(tracker.segments[0]["training_eligible"])
        self.assertEqual(
            filter_training_rows(rows, tracker.segments, tracker.summaries), [],
        )

    def test_game_invalid_segment_is_ineligible(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(frame=1, invalid=1))
        tracker.process_sample(make_sample(frame=2, current=0.6, session_time=10.016, invalid=1))
        tracker.finish()
        self.assertFalse(tracker.segments[0]["segment_valid"])
        self.assertFalse(tracker.segments[0]["training_eligible"])

    def test_segment_ids_increase_and_sample_index_resets(self):
        tracker = LapTracker(1, 0)
        first, _ = tracker.process_sample(make_sample(frame=30))
        tracker.process_sample(make_sample(frame=31, current=0.6, session_time=10.016))
        third, _ = tracker.process_sample(make_sample(frame=10, current=0.7, session_time=10.032))
        self.assertEqual((first["segment_id"], first["segment_sample_index"]), (1, 0))
        self.assertEqual((third["segment_id"], third["segment_sample_index"]), (2, 0))

    def test_segment_summary_has_distance_duration_and_count(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(frame=1, distance=100, session_time=10))
        tracker.process_sample(make_sample(frame=2, current=0.6, distance=120, session_time=10.016))
        tracker.finish()
        summary = tracker.segments[0]
        self.assertEqual(summary["start_distance_m"], 100)
        self.assertEqual(summary["end_distance_m"], 120)
        self.assertEqual(summary["sample_count"], 2)
        self.assertAlmostEqual(summary["segment_duration_s"], 0.016)
        self.assertTrue(summary["training_eligible"])

    def test_flashback_closes_segment_without_removing_pre_event_samples(self):
        tracker = LapTracker(1, 0)
        retained = []
        retained.append(tracker.process_sample(make_sample(frame=10, current=10))[0])
        retained.append(tracker.process_sample(make_sample(frame=11, current=11, session_time=10.016))[0])
        tracker.process_event({"event_code": "FLBK", "flashback_frame_id": 5, "flashback_session_time_s": 9})
        ignored, _ = tracker.process_sample(make_sample(frame=5, current=9, session_time=10.032, distance=90))
        after, _ = tracker.process_sample(make_sample(frame=6, current=9.016, session_time=10.048, distance=91))
        self.assertIsNone(ignored)
        retained.append(after)
        self.assertEqual(len(retained), 3)
        self.assertEqual(tracker.segments[0]["segment_end_reason"], "flashback")
        self.assertEqual(after["segment_id"], 2)
        self.assertEqual(tracker.flashback_events[0]["target_frame_id"], 5)

    def test_flashback_does_not_reconstruct_a_clean_lap(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(lap=1, current=0.5, frame=1))
        tracker.process_sample(make_sample(lap=1, current=20, frame=2, session_time=10.016))
        tracker.process_event({"event_code": "FLBK", "flashback_frame_id": 1, "flashback_session_time_s": 10})
        tracker.process_sample(make_sample(lap=1, current=10, frame=1, session_time=10.032, distance=100))
        tracker.process_sample(make_sample(lap=1, current=10.016, frame=2, session_time=10.048, distance=101))
        _, completed = tracker.process_sample(make_sample(
            lap=2, current=0.2, last=92, frame=3, session_time=10.064, distance=0,
        ))
        self.assertFalse(completed["continuous"])
        self.assertFalse(completed["complete"])
        self.assertFalse(completed["training_eligible"])
        self.assertEqual(completed["segment_count"], 2)

    def test_segment_end_and_start_reasons_are_recorded(self):
        tracker = LapTracker(1, 0)
        tracker.process_sample(make_sample(frame=1))
        tracker.process_event({"event_code": "FLBK", "flashback_frame_id": 1, "flashback_session_time_s": 10})
        tracker.process_sample(make_sample(frame=1, current=1, session_time=10.016))
        tracker.process_sample(make_sample(frame=2, current=1.016, session_time=10.032))
        tracker.finish()
        self.assertEqual(tracker.segments[0]["segment_end_reason"], "flashback")
        self.assertEqual(tracker.segments[1]["segment_start_reason"], "flashback_recovery")


class PacketAndOutputTests(unittest.TestCase):
    def test_all_listed_2026_packet_sizes_validate(self):
        for packet_id, size in PACKET_SIZES_2026.items():
            packet = make_packet(packet_id)
            self.assertEqual(len(packet), size)
            self.assertTrue(valid_packet(bytes(packet), 2026, packet_id))

    def test_session_decoder_reads_session_fields(self):
        packet = make_packet(1)
        struct.pack_into("<BbbBHBb", packet, 29, 3, 31, 22, 57, 5278, 10, 7)
        struct.pack_into("<B", packet, 153, 2)
        struct.pack_into("<ff", packet, 745, 1500.5, 3200.25)

        self.assertEqual(decode_session(bytes(packet)), {
            "weather_code": 3,
            "weather_label": "light_rain",
            "track_temp_c": 31,
            "air_temp_c": 22,
            "total_laps": 57,
            "track_length_m": 5278,
            "session_type_code": 10,
            "track_id": 7,
            "safety_car_status": 2,
            "sector2_start_m": 1500.5,
            "sector3_start_m": 3200.25,
        })

    def test_session_decoder_preserves_unknown_weather_code(self):
        packet = make_packet(1)
        packet[29] = 9

        decoded = decode_session(bytes(packet))

        self.assertEqual(decoded["weather_code"], 9)
        self.assertEqual(decoded["weather_label"], "unknown")

    def test_telemetry_motion_status_and_damage_decoders_preserve_fields(self):
        telemetry = telemetry_packet()
        self.assertEqual(decode_telemetry(telemetry, 0)["speed_kmh"], 250)
        self.assertEqual(decode_telemetry(telemetry, 0)["brake_pct"], 50)
        self.assertEqual(decode_telemetry(telemetry, 0)["tyre_surface_temp_rl_c"], 70)
        self.assertIn("world_x_m", decode_motion(bytes(make_packet(0)), 0))
        self.assertIn("velocity_x_mps", decode_motion(bytes(make_packet(0)), 0))
        self.assertIn("yaw_rad", decode_motion(bytes(make_packet(0)), 0))
        self.assertIn("tyre_compound_actual", decode_status(bytes(make_packet(7)), 0))
        damage = decode_damage(bytes(make_packet(10)), 0)
        self.assertIn("tyre_wear_fl_pct", damage)
        self.assertIn("tyre_damage_fr_pct", damage)
        self.assertIn("tyre_blistering_rl_pct", damage)

    def test_damage_decoder_maps_each_field_group_to_correct_wheel(self):
        packet = make_packet(10, player=2)
        offset = HEADER.size + 2 * 46
        struct.pack_into(
            "<4f12B", packet, offset,
            1.25, 2.5, 3.75, 4.5,      # tyre wear: RL, RR, FL, FR
            11, 12, 13, 14,            # tyre damage
            21, 22, 23, 24,            # brake damage
            31, 32, 33, 34,            # tyre blistering
        )
        damage = decode_damage(bytes(packet), 2)

        expected = {
            "tyre_wear_rl_pct": 1.25, "tyre_wear_rr_pct": 2.5,
            "tyre_wear_fl_pct": 3.75, "tyre_wear_fr_pct": 4.5,
            "tyre_damage_rl_pct": 11, "tyre_damage_rr_pct": 12,
            "tyre_damage_fl_pct": 13, "tyre_damage_fr_pct": 14,
            "brake_damage_rl_pct": 21, "brake_damage_rr_pct": 22,
            "brake_damage_fl_pct": 23, "brake_damage_fr_pct": 24,
            "tyre_blistering_rl_pct": 31, "tyre_blistering_rr_pct": 32,
            "tyre_blistering_fl_pct": 33, "tyre_blistering_fr_pct": 34,
        }
        self.assertEqual(damage, expected)

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=SAMPLE_FIELDS)
        writer.writeheader()
        writer.writerow({field: damage.get(field, "") for field in SAMPLE_FIELDS})
        output.seek(0)
        csv_row = next(csv.DictReader(output))
        for wheel, value in zip(WHEELS, (21, 22, 23, 24)):
            self.assertEqual(csv_row[f"brake_damage_{wheel}_pct"], str(value))

    def test_brake_damage_columns_are_in_sample_fields(self):
        from decode_laps import SAMPLE_FIELDS as OFFLINE_FIELDS

        for wheel in WHEELS:
            field = f"brake_damage_{wheel}_pct"
            self.assertIn(field, SAMPLE_FIELDS)
            self.assertIn(field, OFFLINE_FIELDS)

    def test_lap_decoder_preserves_distance_time_and_invalid_flag(self):
        decoded = decode_lap(lap_packet(
            lap=7, current_ms=1500, last_ms=92000, distance=345.5, invalid=1,
        ), 0)
        self.assertEqual(decoded["lap_number"], 7)
        self.assertEqual(decoded["current_lap_time_s"], 1.5)
        self.assertEqual(decoded["last_lap_time_s"], 92.0)
        self.assertEqual(decoded["lap_distance_m"], 345.5)
        self.assertEqual(decoded["lap_invalid"], 1)

    def test_pipeline_caches_are_isolated_by_session_and_vehicle(self):
        pipeline = PipelineState()
        pipeline.process(telemetry_packet(speed=111, session=1, frame=1, player=0))
        pipeline.process(telemetry_packet(speed=222, session=1, frame=1, player=1))
        pipeline.process(telemetry_packet(speed=333, session=2, frame=1, player=0))
        self.assertEqual(pipeline.latest_telemetry[(1, 0)]["speed_kmh"], 111)
        self.assertEqual(pipeline.latest_telemetry[(1, 1)]["speed_kmh"], 222)
        self.assertEqual(pipeline.latest_telemetry[(2, 0)]["speed_kmh"], 333)

    def test_car_setup_packet_decodes_setup_and_next_front_wing(self):
        packet = make_packet(5)
        values = tuple(range(4)) + (
            1.0, 2.0, 3.0, 4.0,
        ) + tuple(range(9)) + (
            5.0, 6.0, 7.0, 8.0, 9, 12.0,
        )
        CAR_SETUP.pack_into(packet, HEADER.size, *values)
        struct.pack_into("<f", packet, HEADER.size + 24 * CAR_SETUP.size, 13.5)

        result = decode_car_setup(bytes(packet), 0)

        self.assertEqual(result["front_wing"], 0)
        self.assertEqual(result["front_camber"], 1.0)
        self.assertEqual(result["fuel_load_kg"], 12.0)
        self.assertEqual(result["next_front_wing_value"], 13.5)

    def test_pipeline_synchronizes_lap_and_latest_telemetry(self):
        pipeline = PipelineState()
        pipeline.process(lap_packet(frame=1))
        result = pipeline.process(telemetry_packet(speed=225, frame=1))
        if result is None:
            self.fail("Telemetry packet should produce a sample when lap data is available")
        self.assertEqual(result["kind"], "telemetry")
        self.assertEqual(result["speed_kmh"], 225)
        self.assertEqual(result["segment_id"], 1)
        self.assertEqual(result["segment_sample_index"], 0)

    def test_pipeline_attaches_latest_shared_session_data(self):
        session_packet = make_packet(1, session=7, frame=3)
        struct.pack_into("<BbbBHBb", session_packet, 29, 4, 32, 21, 58, 5300, 10, 7)
        struct.pack_into("<B", session_packet, 153, 2)
        struct.pack_into("<ff", session_packet, 745, 1500.5, 3200.25)
        pipeline = PipelineState()
        pipeline.process(bytes(session_packet), received_ns=300)
        pipeline.process(lap_packet(session=7, frame=4))

        result = pipeline.process(
            telemetry_packet(session=7, speed=225, frame=5), received_ns=500
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "telemetry")
        self.assertEqual(result["weather_code"], 4)
        self.assertEqual(result["track_id"], 7)
        self.assertEqual(result["session_frame_id"], 3)

    def test_pipeline_leaves_session_fields_absent_without_session_packet(self):
        pipeline = PipelineState()
        pipeline.process(lap_packet(frame=1))

        result = pipeline.process(telemetry_packet(speed=225, frame=2))

        self.assertIsNotNone(result)
        self.assertIn(result.get("weather_code"), (None, ""))
        self.assertIn(result.get("weather_label", ""), (None, ""))

    def test_sample_fields_include_shared_session_columns(self):
        session_fields = (
            "weather_code", "weather_label", "track_temp_c", "air_temp_c",
            "total_laps", "track_length_m", "session_type_code", "track_id",
            "safety_car_status", "sector2_start_m", "sector3_start_m",
            "session_frame_id", "session_received_ns",
        )
        for field in session_fields:
            self.assertIn(field, SAMPLE_FIELDS)

    def test_pipeline_records_flashback_as_diagnostic_event(self):
        pipeline = PipelineState()
        pipeline.process(lap_packet(frame=1))
        pipeline.process(telemetry_packet(frame=1))
        result = pipeline.process(flashback_packet(target_frame=1, target_time=1))
        if result is None:
            self.fail("Flashback packet should produce a diagnostic event")
        self.assertEqual(result["kind"], "flashback")
        tracker = pipeline.lap_states[(1, 0)]
        self.assertEqual(tracker.segments[0]["segment_end_reason"], "flashback")
        self.assertEqual(len(tracker.flashback_events), 1)
        self.assertEqual(tracker.flashback_events[0]["target_frame_id"], 1)

    def test_live_sample_keeps_motion_and_component_timestamps(self):
        meta = packet_meta(telemetry_packet(frame=4))
        row = build_telemetry_row(
            400,
            meta,
            {"lap_number": 1, "lap_received_ns": 390},
            {"status_frame_id": 2, "status_received_ns": 200},
            {"damage_frame_id": 3, "damage_received_ns": 300},
            {"speed_kmh": 200, "telemetry_frame_id": 4, "telemetry_received_ns": 400},
            {"world_x_m": 10, "motion_frame_id": 2, "motion_received_ns": 210},
        )
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=SAMPLE_FIELDS)
        writer.writeheader()
        writer.writerow(row)
        self.assertIn("segment_id", output.getvalue())
        self.assertEqual(row["world_x_m"], 10)
        self.assertEqual(row["motion_received_ns"], 210)
        self.assertEqual(row["damage_frame_id"], 3)

    def test_braking_events_reset_at_segment_and_lap_boundaries(self):
        rows = [
            {"session_uid": 1, "vehicle_index": 0, "lap_number": 1, "segment_id": 1, "brake_pct": 20},
            {"session_uid": 1, "vehicle_index": 0, "lap_number": 1, "segment_id": 2, "brake_pct": 20},
            {"session_uid": 1, "vehicle_index": 0, "lap_number": 2, "segment_id": 3, "brake_pct": 20},
        ]
        self.assertEqual(len(find_braking_events(rows, 5)), 3)

    def test_live_braking_builder_resets_on_segment_change(self):
        values = {"session_uid": 1, "vehicle_index": 0, "lap_number": 1, "segment_id": 2, "brake_pct": 30}
        event, brake, key = build_braking_event(values, 30, (1, 0, 1, 1))
        if event is None:
            self.fail("A segment-boundary brake threshold crossing should be emitted")
        self.assertEqual(event["segment_id"], 2)
        self.assertEqual(brake, 30)
        self.assertEqual(key, (1, 0, 1, 2))

    def test_offline_decoder_reads_raw_binary_without_sidecar_csv(self):
        packets = [
            (1_000_000_000, lap_packet(lap=1, frame=1, session_time=10.0)),
            (1_016_000_000, telemetry_packet(speed=201, frame=2)),
            (1_032_000_000, lap_packet(lap=1, current_ms=1000, frame=3, session_time=10.032)),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.bin"
            with path.open("wb") as file:
                for received_ns, packet in packets:
                    file.write(struct.pack("<QI", received_ns, len(packet)))
                    file.write(packet)
            samples = read_recording(path)
        self.assertEqual(len(samples), 1)
        self.assertIn("segment_id", samples[0])
        self.assertEqual(samples[0]["speed_kmh"], 201)

    def test_raw_decoder_uses_lap_cache_when_emitting_telemetry_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.bin"
            packets = [lap_packet(frame=1), telemetry_packet(speed=175, frame=2)]
            path.write_bytes(b"".join(
                struct.pack("<QI", index, len(packet)) + packet
                for index, packet in enumerate(packets, start=1)
            ))
            rows = read_recording(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lap_number"], 1)
        self.assertEqual(rows[0]["speed_kmh"], 175)

    def test_raw_decoder_attaches_session_packet_to_telemetry_row(self):
        session_packet = make_packet(1, session=3, frame=1)
        struct.pack_into("<BbbBHBb", session_packet, 29, 3, 31, 22, 57, 5278, 10, 7)
        struct.pack_into("<B", session_packet, 153, 2)
        struct.pack_into("<ff", session_packet, 745, 1500.5, 3200.25)
        packets = [
            bytes(session_packet),
            lap_packet(session=3, frame=2),
            telemetry_packet(session=3, speed=201, frame=3),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.bin"
            path.write_bytes(b"".join(
                struct.pack("<QI", index, len(packet)) + packet
                for index, packet in enumerate(packets, start=1)
            ))

            rows = read_recording(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["weather_code"], 3)

    def test_offline_decoder_writes_only_complete_laps_to_training_csv(self):
        packets = [
            lap_packet(lap=1, current_ms=500, frame=1),
            telemetry_packet(speed=200, frame=2),
            lap_packet(lap=1, current_ms=1000, distance=100, frame=3),
            telemetry_packet(speed=201, frame=4),
            lap_packet(lap=2, current_ms=200, last_ms=92000, frame=5),
            telemetry_packet(speed=202, frame=6),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.bin"
            path.write_bytes(b"".join(
                struct.pack("<QI", index, len(packet)) + packet
                for index, packet in enumerate(packets, start=1)
            ))
            with patch("sys.argv", ["decode_laps", str(path)]):
                decode_laps_main()

            with path.with_name("training_laps.csv").open(newline="") as file:
                training_rows = list(csv.DictReader(file))
            with path.with_name("laps.csv").open(newline="") as file:
                all_rows = list(csv.DictReader(file))

        self.assertEqual(len(training_rows), 2)
        self.assertEqual({row["lap_number"] for row in training_rows}, {"1"})
        self.assertGreater(len(all_rows), len(training_rows))


if __name__ == "__main__":
    unittest.main()
