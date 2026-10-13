from dataclasses import dataclass
from typing import Optional

from telemetry_common import classify_compound, classify_weather, lap_condition_class


FRAME_REGRESSION_THRESHOLD = 5
SESSION_TIME_REGRESSION_TOLERANCE_S = 0.05
LAP_TIME_REGRESSION_THRESHOLD_S = 0.5
DISTANCE_REGRESSION_THRESHOLD_M = 50.0
PACKET_GAP_THRESHOLD_S = 0.75
PACKET_GAP_FRAME_THRESHOLD = 45
DUPLICATE_TIME_TOLERANCE_S = 0.001
DUPLICATE_DISTANCE_TOLERANCE_M = 0.25
FREEZE_TIMEOUT_NS = 1_000_000_000
MIN_SEGMENT_SAMPLES = 2
LAP_START_WINDOW_S = 2.0
LOW_SPEED_KMH = 8.0
LOW_SPEED_DURATION_S = 3.0


@dataclass
class Detection:
    accepted: bool = True
    end_reason: str = ""
    resumed: bool = False


class DiscontinuityDetector:
    """Identify breaks in simulated progression without interpreting rewind targets."""

    def __init__(self):
        self.previous: Optional[dict] = None
        self.duplicate_since_ns: Optional[int] = None
        self.frozen = False
        self.ignore_regression_once = False
        self.awaiting_progress = False

    @staticmethod
    def _value(sample, key):
        value = sample.get(key)
        return float(value) if value not in (None, "") else None

    def _same_progress(self, sample, previous):
        comparisons = (
            ("session_time_s", DUPLICATE_TIME_TOLERANCE_S),
            ("current_lap_time_s", DUPLICATE_TIME_TOLERANCE_S),
            ("lap_distance_m", DUPLICATE_DISTANCE_TOLERANCE_M),
        )
        comparable = 0
        for field, tolerance in comparisons:
            current_value = self._value(sample, field)
            previous_value = self._value(previous, field)
            if current_value is not None and previous_value is not None:
                comparable += 1
                if abs(current_value - previous_value) > tolerance:
                    return False
        if comparable:
            return True
        return sample.get("frame_id") == previous.get("frame_id")

    def _is_normal_lap_transition(self, sample, previous):
        lap_number = sample.get("lap_number")
        previous_lap = previous.get("lap_number")
        current_time = self._value(sample, "current_lap_time_s")
        return bool(
            lap_number is not None
            and previous_lap is not None
            and lap_number > previous_lap
            and current_time is not None
            and current_time < LAP_START_WINDOW_S
        )

    def _regression_reason(self, sample, previous):
        frame = sample.get("frame_id")
        previous_frame = previous.get("frame_id")
        if (
            frame is not None
            and previous_frame is not None
            and previous_frame - frame > FRAME_REGRESSION_THRESHOLD
        ):
            return "frame_regression"

        session_time = self._value(sample, "session_time_s")
        previous_session_time = self._value(previous, "session_time_s")
        if (
            session_time is not None
            and previous_session_time is not None
            and session_time < previous_session_time - SESSION_TIME_REGRESSION_TOLERANCE_S
        ):
            return "session_time_regression"

        if self._is_normal_lap_transition(sample, previous):
            return ""

        lap_number = sample.get("lap_number")
        previous_lap = previous.get("lap_number")
        same_lap = lap_number == previous_lap
        current_time = self._value(sample, "current_lap_time_s")
        previous_time = self._value(previous, "current_lap_time_s")
        if (
            same_lap
            and current_time is not None
            and previous_time is not None
            and current_time < previous_time - LAP_TIME_REGRESSION_THRESHOLD_S
        ):
            return "lap_time_regression"

        distance = self._value(sample, "lap_distance_m")
        previous_distance = self._value(previous, "lap_distance_m")
        if (
            same_lap
            and distance is not None
            and previous_distance is not None
            and distance < previous_distance - DISTANCE_REGRESSION_THRESHOLD_M
        ):
            return "distance_regression"

        if (
            session_time is not None
            and previous_session_time is not None
            and session_time - previous_session_time > PACKET_GAP_THRESHOLD_S
        ):
            return "packet_gap"
        if (
            frame is not None
            and previous_frame is not None
            and frame - previous_frame > PACKET_GAP_FRAME_THRESHOLD
        ):
            return "packet_gap"
        return ""

    def inspect(self, sample):
        previous = self.previous
        if previous is None:
            self.previous = dict(sample)
            if self.awaiting_progress:
                self.awaiting_progress = False
                return Detection(accepted=False)
            return Detection()

        reason = ""
        if self.ignore_regression_once:
            self.ignore_regression_once = False
        else:
            reason = self._regression_reason(sample, previous)
        if reason:
            self.previous = dict(sample)
            self.duplicate_since_ns = None
            self.frozen = False
            return Detection(end_reason=reason)

        received_ns = sample.get("received_ns")
        if self._same_progress(sample, previous):
            if self.duplicate_since_ns is None and received_ns is not None:
                self.duplicate_since_ns = int(received_ns)
            if self.frozen:
                return Detection(accepted=False)
            if (
                received_ns is not None
                and self.duplicate_since_ns is not None
                and int(received_ns) - self.duplicate_since_ns >= FREEZE_TIMEOUT_NS
            ):
                self.frozen = True
                return Detection(accepted=False, end_reason="telemetry_freeze")
            return Detection(accepted=False)

        was_frozen = self.frozen
        self.frozen = False
        self.duplicate_since_ns = None

        self.previous = dict(sample)
        return Detection(resumed=was_frozen)

    def reset_after_flashback(self):
        self.previous = None
        self.duplicate_since_ns = None
        self.frozen = False
        self.ignore_regression_once = True
        self.awaiting_progress = True


class SegmentTracker:
    """Assign samples to independent continuous telemetry segments."""

    def __init__(self, session_uid=None, vehicle_index=None):
        self.session_uid = session_uid
        self.vehicle_index = vehicle_index
        self.next_segment_id = 1
        self.current: Optional[dict] = None
        self.segments = []

    def start(self, sample, reason):
        self.current = {
            "session_uid": sample.get("session_uid", self.session_uid),
            "vehicle_index": sample.get("vehicle_index", self.vehicle_index),
            "lap_number": sample.get("lap_number", ""),
            "segment_id": self.next_segment_id,
            "segment_sample_index": 0,
            "segment_start_reason": reason or "session_start",
            "segment_valid": not bool(sample.get("lap_invalid")),
            "sample_count": 0,
            "first_sample": dict(sample),
            "last_sample": dict(sample),
        }
        self.next_segment_id += 1

    def add(self, sample, start_reason=""):
        started = self.current is None
        if started:
            self.start(sample, start_reason)
        segment = self.current
        if segment is None:
            raise RuntimeError("Could not start telemetry segment")
        sample.update({
            "segment_id": segment["segment_id"],
            "segment_sample_index": segment["segment_sample_index"],
            "segment_valid": segment["segment_valid"] and not bool(sample.get("lap_invalid")),
            "training_eligible": segment["segment_valid"] and not bool(sample.get("lap_invalid")),
        })
        if sample.get("lap_invalid"):
            segment["segment_valid"] = False
        segment["sample_count"] += 1
        segment["segment_sample_index"] += 1
        segment["last_sample"] = dict(sample)
        return sample, started

    def mark_invalid(self):
        if self.current is not None:
            self.current["segment_valid"] = False

    @staticmethod
    def _metadata(sample, prefix):
        return {
            f"{prefix}_frame_id": sample.get("frame_id", ""),
            f"{prefix}_session_time_s": sample.get("session_time_s", ""),
            f"{prefix}_distance_m": sample.get("lap_distance_m", ""),
        }

    def close(self, reason):
        if self.current is None:
            return None
        segment = self.current
        first = segment["first_sample"]
        last = segment["last_sample"]
        start_time = self._value(first, "session_time_s")
        end_time = self._value(last, "session_time_s")
        start_distance = self._value(first, "lap_distance_m")
        end_distance = self._value(last, "lap_distance_m")
        count = segment["sample_count"]
        summary = {
            "session_uid": segment["session_uid"],
            "vehicle_index": segment["vehicle_index"],
            "lap_number": segment["lap_number"],
            "segment_id": segment["segment_id"],
            **self._metadata(first, "start"),
            **self._metadata(last, "end"),
            "segment_duration_s": (
                end_time - start_time
                if start_time is not None and end_time is not None
                else ""
            ),
            "sample_count": count,
            "segment_start_reason": segment["segment_start_reason"],
            "segment_end_reason": reason,
            "segment_valid": segment["segment_valid"],
            "training_eligible": segment["segment_valid"] and count >= MIN_SEGMENT_SAMPLES,
        }
        summary["coverage_distance_m"] = (
            end_distance - start_distance
            if start_distance is not None and end_distance is not None
            else ""
        )
        self.segments.append(summary)
        self.current = None
        return summary

    @staticmethod
    def _value(sample, key):
        value = sample.get(key)
        return float(value) if value not in (None, "") else None


class LapTracker:
    """Coordinate lap completion with continuity-aware segment tracking."""

    def __init__(self, session_uid=None, vehicle_index=None):
        self.session_uid = session_uid
        self.vehicle_index = vehicle_index
        self.detector = DiscontinuityDetector()
        self.segment_tracker = SegmentTracker(session_uid, vehicle_index)
        self.summaries = []
        self.flashback_events = []
        self.active = False
        self.lap_number: Optional[int] = None
        self.invalid = False
        self.exclusion_reason = ""
        self.continuous = True
        self.lap_start_known = False
        self.lap_segment_count = 0
        self.low_speed_s = 0.0
        self.weather_codes_seen = set()
        self.weather_classes_seen = set()
        self.compound_codes_seen = set()
        self.compound_classes_seen = set()
        self.previous_sample: Optional[dict] = None
        self.pending_start_reason = "session_start"

    @property
    def segments(self):
        return self.segment_tracker.segments

    def _begin_lap(self, sample, known_start):
        self.active = True
        self.lap_number = sample.get("lap_number")
        self.invalid = bool(sample.get("lap_invalid"))
        self.exclusion_reason = "game_marked_invalid" if self.invalid else ""
        self.continuous = True
        self.lap_start_known = known_start
        self.lap_segment_count = 0
        self.low_speed_s = 0.0
        self.weather_codes_seen = set()
        self.weather_classes_seen = set()
        self.compound_codes_seen = set()
        self.compound_classes_seen = set()

    @staticmethod
    def _codes_seen_string(codes):
        def sort_key(code):
            try:
                return (0, int(code))
            except ValueError:
                return (1, code)

        return "|".join(sorted(codes, key=sort_key))

    def _record_lap_conditions(self, sample):
        weather_code = sample.get("weather_code")
        compound_code = sample.get("tyre_compound_actual")
        self.weather_classes_seen.add(classify_weather(weather_code))
        self.compound_classes_seen.add(classify_compound(compound_code))
        if weather_code not in (None, ""):
            self.weather_codes_seen.add(str(weather_code).strip())
        if compound_code not in (None, ""):
            self.compound_codes_seen.add(str(compound_code).strip())

    def _lap_condition_fields(self):
        return {
            "weather_codes_seen": self._codes_seen_string(self.weather_codes_seen),
            "weather_changed_in_lap": len(self.weather_codes_seen) > 1,
            "compound_codes_seen": self._codes_seen_string(self.compound_codes_seen),
            "condition_class": lap_condition_class(
                self.weather_classes_seen, self.compound_classes_seen
            ),
        }

    def _complete_lap(self, sample):
        complete = self.continuous and self.lap_start_known
        valid = complete and not self.invalid
        summary = {
            "session_uid": self.session_uid,
            "vehicle_index": self.vehicle_index,
            "lap_number": self.lap_number,
            "lap_time_s": sample.get("last_lap_time_s", ""),
            "valid": valid,
            "complete": complete,
            "continuous": self.continuous,
            "training_eligible": valid,
            "segment_count": self.lap_segment_count,
            "exclusion_reason": self.exclusion_reason or (
                "discontinuity" if not self.continuous else ""
            ),
            **self._lap_condition_fields(),
        }
        self.summaries.append(summary)
        return summary

    def process_event(
        self, event, source_frame_id=None, source_session_time_s=None
    ):
        if event.get("event_code") != "FLBK":
            return None
        current_sample = self.previous_sample or {}
        self.flashback_events.append({
            "session_uid": self.session_uid,
            "vehicle_index": self.vehicle_index,
            "source_frame_id": source_frame_id if source_frame_id is not None else current_sample.get("frame_id", ""),
            "source_session_time_s": source_session_time_s if source_session_time_s is not None else current_sample.get("session_time_s", ""),
            "target_frame_id": event.get("flashback_frame_id", ""),
            "target_session_time_s": event.get("flashback_session_time_s", ""),
            "lap_number": self.lap_number if self.lap_number is not None else "",
        })
        if self.active:
            self.continuous = False
            self.exclusion_reason = self.exclusion_reason or "flashback"
        self.segment_tracker.close("flashback")
        self.pending_start_reason = "flashback_recovery"
        self.detector.reset_after_flashback()
        return self.flashback_events[-1]

    def process_sample(self, source_sample):
        sample = dict(source_sample)
        session_uid = sample.get("session_uid", self.session_uid)
        vehicle_index = sample.get("vehicle_index", self.vehicle_index)
        if self.session_uid is None:
            self.session_uid = session_uid
        if self.vehicle_index is None:
            self.vehicle_index = vehicle_index
        if (session_uid, vehicle_index) != (self.session_uid, self.vehicle_index):
            self.__init__(session_uid, vehicle_index)

        detection = self.detector.inspect(sample)
        if not detection.accepted:
            if detection.end_reason:
                if self.active:
                    self.continuous = False
                    self.exclusion_reason = self.exclusion_reason or detection.end_reason
                self.segment_tracker.close(detection.end_reason)
                self.pending_start_reason = "telemetry_resume"
            return None, None

        if detection.end_reason:
            if self.active:
                self.continuous = False
                self.exclusion_reason = self.exclusion_reason or detection.end_reason
            self.segment_tracker.close(detection.end_reason)
            self.pending_start_reason = detection.end_reason
        elif detection.resumed:
            self.pending_start_reason = "telemetry_resume"

        completed = None
        lap_number = sample.get("lap_number")
        current_time = sample.get("current_lap_time_s")
        current_time = float(current_time) if current_time not in (None, "") else None
        normal_crossing = bool(
            not detection.end_reason
            and self.active
            and lap_number is not None
            and self.lap_number is not None
            and lap_number > self.lap_number
            and current_time is not None
            and current_time < LAP_START_WINDOW_S
        )
        if normal_crossing:
            self.segment_tracker.close("game_invalid" if self.invalid else "lap_complete")
            completed = self._complete_lap(sample)
            self._begin_lap(sample, known_start=True)
            self.pending_start_reason = "lap_start"
        elif not self.active and lap_number is not None:
            self._begin_lap(
                sample,
                known_start=current_time is not None and current_time < LAP_START_WINDOW_S,
            )

        if self.active:
            self._record_lap_conditions(sample)
            previous_time = self.previous_sample.get("session_time_s") if self.previous_sample else None
            current_session_time = sample.get("session_time_s")
            elapsed = 0.0
            if previous_time is not None and current_session_time is not None:
                elapsed = float(current_session_time) - float(previous_time)
            speed = sample.get("speed_kmh")
            brake = sample.get("brake_pct", 0.0) or 0.0
            throttle = sample.get("throttle_pct", 0.0) or 0.0
            if (
                speed is not None
                and float(speed) < LOW_SPEED_KMH
                and sample.get("pit_status", 0) == 0
                and float(brake) < 5.0
                and float(throttle) < 20.0
                and 0.0 < elapsed <= 0.5
            ):
                self.low_speed_s += elapsed
                if self.low_speed_s >= LOW_SPEED_DURATION_S:
                    self.invalid = True
                    self.exclusion_reason = self.exclusion_reason or "extended_low_speed"
                    self.segment_tracker.mark_invalid()
            elif speed is None or float(speed) >= LOW_SPEED_KMH or sample.get("pit_status", 0) != 0:
                self.low_speed_s = 0.0

        if self.active and sample.get("lap_invalid"):
            self.invalid = True
            self.exclusion_reason = self.exclusion_reason or "game_marked_invalid"
            self.segment_tracker.mark_invalid()

        sample["lap_continuous"] = self.continuous if self.active else False
        sample, started = self.segment_tracker.add(sample, self.pending_start_reason)
        if started:
            self.pending_start_reason = ""
            if self.active:
                self.lap_segment_count += 1
        self.previous_sample = dict(sample)
        return sample, completed

    def current_metadata(self):
        current = self.segment_tracker.current or {}
        return {
            "segment_id": current.get("segment_id", ""),
            "segment_sample_index": current.get("segment_sample_index", ""),
            "lap_number": self.lap_number if self.lap_number is not None else "",
        }

    def finish(self):
        self.segment_tracker.close("recording_end")
        if self.active:
            self.summaries.append({
                "session_uid": self.session_uid,
                "vehicle_index": self.vehicle_index,
                "lap_number": self.lap_number,
                "lap_time_s": "",
                "valid": False,
                "complete": False,
                "continuous": self.continuous,
                "training_eligible": False,
                "segment_count": self.lap_segment_count,
                "exclusion_reason": self.exclusion_reason or "recording_end",
                **self._lap_condition_fields(),
            })
            self.active = False
