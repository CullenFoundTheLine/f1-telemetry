from dataclasses import dataclass, field
from typing import Optional

from lap_tracking import LapTracker
from telemetry_common import (
    decode_car_setup,
    decode_damage,
    decode_flashback,
    decode_lap,
    decode_motion,
    decode_session,
    decode_status,
    decode_telemetry,
    packet_meta,
    valid_packet,
)


@dataclass
class PipelineState:
    lap_states: dict = field(default_factory=dict)
    latest_laps: dict = field(default_factory=dict)
    latest_motion: dict = field(default_factory=dict)
    latest_status: dict = field(default_factory=dict)
    latest_damage: dict = field(default_factory=dict)
    latest_telemetry: dict = field(default_factory=dict)
    latest_setup: dict = field(default_factory=dict)
    latest_session: dict = field(default_factory=dict)

    def process(self, packet: bytes, received_ns: int = 0) -> Optional[dict]:
        meta = packet_meta(packet)
        if meta is None or not valid_packet(packet, 2026, meta.packet_id):
            return None
        if meta.packet_id != 1 and not 0 <= meta.player_index < 24:
            return None
        if meta.packet_id == 1:
            session = {
                **decode_session(packet),
                "session_frame_id": meta.frame_id,
                "session_received_ns": received_ns,
            }
            self.latest_session[meta.session_uid] = session
            return {"kind": "session", **session}
        key = (meta.session_uid, meta.player_index)
        if meta.packet_id == 3:
            event = decode_flashback(packet)
            if event["event_code"] != "FLBK":
                return {"kind": "event", **event}
            tracker = self.lap_states.setdefault(
                key, LapTracker(meta.session_uid, meta.player_index)
            )
            result = tracker.process_event(
                event,
                source_frame_id=meta.frame_id,
                source_session_time_s=meta.session_time_s,
            ) or {}
            return {"kind": "flashback", **event, **result}
        if meta.packet_id == 0:
            motion = {
                **decode_motion(packet, meta.player_index),
                "motion_frame_id": meta.frame_id,
                "motion_received_ns": received_ns,
            }
            self.latest_motion[key] = motion
            return {"kind": "position", **motion}
        if meta.packet_id == 2:
            lap = {
                **decode_lap(packet, meta.player_index),
                "session_uid": meta.session_uid,
                "lap_frame_id": meta.frame_id,
                "lap_received_ns": received_ns,
            }
            self.latest_laps[key] = lap
            return {"kind": "lap", **lap}
        if meta.packet_id == 5:
            setup = {
                **decode_car_setup(packet, meta.player_index),
                "setup_frame_id": meta.frame_id,
                "setup_received_ns": received_ns,
            }
            self.latest_setup[key] = setup
            return {"kind": "setup", **setup}
        if meta.packet_id == 6:
            telemetry = {
                **decode_telemetry(packet, meta.player_index),
                "telemetry_frame_id": meta.frame_id,
                "telemetry_received_ns": received_ns,
            }
            self.latest_telemetry[key] = telemetry
            lap = self.latest_laps.get(key)
            if lap is None:
                return {"kind": "telemetry", **telemetry, "sample_ignored": True}
            tracker = self.lap_states.setdefault(
                key, LapTracker(meta.session_uid, meta.player_index)
            )
            source_sample = {
                **lap,
                **self.latest_motion.get(key, {}),
                **self.latest_status.get(key, {}),
                **self.latest_damage.get(key, {}),
                **self.latest_session.get(meta.session_uid, {}),
                **telemetry,
                "session_uid": meta.session_uid,
                "vehicle_index": meta.player_index,
                "frame_id": meta.frame_id,
                "overall_frame_id": meta.overall_frame_id,
                "session_time_s": meta.session_time_s,
                "received_ns": received_ns,
            }
            sample, completed = tracker.process_sample(source_sample)
            if sample is None:
                return {
                    "kind": "telemetry", "completed": completed,
                    "sample_ignored": True,
                }
            return {"kind": "telemetry", "completed": completed, **sample}
        if meta.packet_id == 7:
            status = {
                **decode_status(packet, meta.player_index),
                "status_frame_id": meta.frame_id,
                "status_received_ns": received_ns,
            }
            self.latest_status[key] = status
            return {"kind": "status", **status}
        if meta.packet_id == 10:
            damage = {
                **decode_damage(packet, meta.player_index),
                "damage_frame_id": meta.frame_id,
                "damage_received_ns": received_ns,
            }
            self.latest_damage[key] = damage
            return {"kind": "damage", **damage}
        # TODO: Decode Car Setup packets from their documented structures.
        return {"kind": "other", "packet_id": meta.packet_id}

    def snapshot(self, session_uid: int, vehicle_index: int) -> dict:
        """Return one dashboard-friendly view of the latest known car state."""
        key = (session_uid, vehicle_index)
        tracker = self.lap_states.get(key)
        return {
            "session_uid": session_uid,
            "vehicle_index": vehicle_index,
            **self.latest_laps.get(key, {}),
            **self.latest_motion.get(key, {}),
            **self.latest_status.get(key, {}),
            **self.latest_damage.get(key, {}),
            **self.latest_telemetry.get(key, {}),
            **self.latest_setup.get(key, {}),
            **(tracker.current_metadata() if tracker else {}),
        }

    def finish(self):
        for tracker in self.lap_states.values():
            tracker.finish()
