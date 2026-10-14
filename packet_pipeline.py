from dataclasses import dataclass, field
from typing import Optional

from lap_tracking import LapTracker
from telemetry_common import (
    FramePacketJoiner,
    decode_car_setup,
    decode_damage,
    decode_flashback,
    decode_lap,
    decode_motion,
    decode_session,
    decode_status,
    decode_telemetry,
    latest_with_frame_age,
    packet_meta,
    valid_packet,
)


@dataclass
class PipelineState:
    lap_states: dict = field(default_factory=dict)
    latest_samples: dict = field(default_factory=dict)
    latest_damage: dict = field(default_factory=dict)
    latest_setup: dict = field(default_factory=dict)
    latest_session: dict = field(default_factory=dict)
    frame_joiner: FramePacketJoiner = field(default_factory=FramePacketJoiner)

    def _add_frame_part(self, meta, received_ns, stream, decoded):
        values = {
            **decoded,
            f"{stream}_frame_id": meta.frame_id,
            f"{stream}_received_ns": received_ns,
        }
        joined = self.frame_joiner.add(
            meta.session_uid, meta.player_index, meta.frame_id, stream, values,
        )
        if joined is None:
            return None

        key = (meta.session_uid, meta.player_index)
        tracker = self.lap_states.setdefault(
            key, LapTracker(meta.session_uid, meta.player_index)
        )
        source_sample = {
            **joined["lap"],
            **joined["motion"],
            **joined["status"],
            **latest_with_frame_age(
                self.latest_damage.get(key, {}), "damage", meta.overall_frame_id,
            ),
            **latest_with_frame_age(
                self.latest_setup.get(key, {}), "setup", meta.overall_frame_id,
            ),
            **latest_with_frame_age(
                self.latest_session.get(meta.session_uid, {}), "session",
                meta.overall_frame_id,
            ),
            **joined["telemetry"],
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
        self.latest_samples[key] = sample
        return {"kind": "telemetry", "completed": completed, **sample}

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
                "_overall_frame_id": meta.overall_frame_id,
            }
            self.latest_session[meta.session_uid] = session
            return {
                "kind": "session",
                **latest_with_frame_age(session, "session", meta.overall_frame_id),
            }
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
            joined = self._add_frame_part(meta, received_ns, "motion", motion)
            if joined is not None:
                return joined
            return {"kind": "position", **motion}
        if meta.packet_id == 2:
            lap = {
                **decode_lap(packet, meta.player_index),
                "session_uid": meta.session_uid,
                "lap_frame_id": meta.frame_id,
                "lap_received_ns": received_ns,
            }
            joined = self._add_frame_part(meta, received_ns, "lap", lap)
            if joined is not None:
                return joined
            return {"kind": "lap", **lap}
        if meta.packet_id == 5:
            setup = {
                **decode_car_setup(packet, meta.player_index),
                "setup_frame_id": meta.frame_id,
                "setup_received_ns": received_ns,
                "_overall_frame_id": meta.overall_frame_id,
            }
            self.latest_setup[key] = setup
            return {
                "kind": "setup",
                **latest_with_frame_age(setup, "setup", meta.overall_frame_id),
            }
        if meta.packet_id == 6:
            telemetry = {
                **decode_telemetry(packet, meta.player_index),
                "telemetry_frame_id": meta.frame_id,
                "telemetry_received_ns": received_ns,
            }
            joined = self._add_frame_part(meta, received_ns, "telemetry", telemetry)
            if joined is None:
                return {"kind": "telemetry", **telemetry, "sample_ignored": True}
            return joined
        if meta.packet_id == 7:
            status = {
                **decode_status(packet, meta.player_index),
                "status_frame_id": meta.frame_id,
                "status_received_ns": received_ns,
            }
            joined = self._add_frame_part(meta, received_ns, "status", status)
            if joined is not None:
                return joined
            return {"kind": "status", **status}
        if meta.packet_id == 10:
            damage = {
                **decode_damage(packet, meta.player_index),
                "damage_frame_id": meta.frame_id,
                "damage_received_ns": received_ns,
                "_overall_frame_id": meta.overall_frame_id,
            }
            self.latest_damage[key] = damage
            return {
                "kind": "damage",
                **latest_with_frame_age(damage, "damage", meta.overall_frame_id),
            }
        return {"kind": "other", "packet_id": meta.packet_id}

    def snapshot(self, session_uid: int, vehicle_index: int) -> dict:
        """Return one dashboard-friendly view of the latest known car state."""
        key = (session_uid, vehicle_index)
        tracker = self.lap_states.get(key)
        latest = self.latest_samples.get(key, {})
        overall_frame_id = latest.get("overall_frame_id")
        return {
            "session_uid": session_uid,
            "vehicle_index": vehicle_index,
            **latest,
            **latest_with_frame_age(
                self.latest_damage.get(key, {}), "damage", overall_frame_id,
            ),
            **latest_with_frame_age(
                self.latest_setup.get(key, {}), "setup", overall_frame_id,
            ),
            **latest_with_frame_age(
                self.latest_session.get(session_uid, {}), "session",
                overall_frame_id,
            ),
            **(tracker.current_metadata() if tracker else {}),
        }

    def finish(self):
        for tracker in self.lap_states.values():
            tracker.finish()
