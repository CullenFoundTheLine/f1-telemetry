import struct
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional


HEADER = struct.Struct("<HBBBBBQfIIBB")
PACKET_SIZES_2026 = {
    0: 1325,
    1: 926,
    2: 1399,
    3: 45,
    4: 1470,
    5: 1233,
    6: 1448,
    7: 1445,
    8: 1134,
    10: 1133,
    11: 1460,
    12: 231,
    13: 273,
    15: 1231,
    16: 269,
}
WEATHER_LABELS = {
    0: "clear",
    1: "light_cloud",
    2: "overcast",
    3: "light_rain",
    4: "heavy_rain",
    5: "storm",
}

TELEMETRY_CAR = struct.Struct("<HfffBbHBBH4H4B4BB4f4B")
MOTION_CAR = struct.Struct("<6f9h3f")
LAP_CAR = struct.Struct("<IIHBHBHBHBfff15BHHBfB")
LEGACY_TELEMETRY_CAR = struct.Struct("<HfffBbH")
FLASHBACK_EVENT = struct.Struct("<4sIf")
CAR_SETUP = struct.Struct("<4B4f9B4fBf")
CAR_SETUP_VALUE_FIELDS = [
    "front_wing", "rear_wing", "on_throttle_differential",
    "off_throttle_differential", "front_camber", "rear_camber",
    "front_toe", "rear_toe", "front_suspension", "rear_suspension",
    "front_anti_roll_bar", "rear_anti_roll_bar", "front_suspension_height",
    "rear_suspension_height", "brake_pressure_pct", "brake_bias_pct",
    "engine_braking_pct", "tyre_pressure_rl_psi", "tyre_pressure_rr_psi",
    "tyre_pressure_fl_psi", "tyre_pressure_fr_psi", "ballast",
    "fuel_load_kg",
]
CAR_SETUP_FIELDS = [
    "received_ns", "session_uid", "session_time_s", "frame_id",
    "overall_frame_id", "vehicle_index",
] + CAR_SETUP_VALUE_FIELDS + ["next_front_wing_value"]

FRAME_JOIN_STREAMS = frozenset({"lap", "motion", "status", "telemetry"})
MAX_DAMAGE_AGE_FRAMES = 6
MAX_SETUP_AGE_FRAMES = 29
MAX_SESSION_AGE_FRAMES = 29
MAX_AGE_FRAMES = {
    "damage": MAX_DAMAGE_AGE_FRAMES,
    "setup": MAX_SETUP_AGE_FRAMES,
    "session": MAX_SESSION_AGE_FRAMES,
}


class FramePacketJoiner:
    """Join the four per-car streams that describe the same simulation frame."""

    def __init__(self, max_pending_frames=120):
        self.max_pending_frames = max_pending_frames
        self.pending = {}
        self.expired = []

    def add(self, session_uid, vehicle_index, frame_id, stream, values, metadata=None):
        if stream not in FRAME_JOIN_STREAMS:
            raise ValueError(f"Unsupported frame-joined stream: {stream}")
        car_key = (session_uid, vehicle_index)
        pending_for_car = self.pending.setdefault(car_key, OrderedDict())
        frame_values = pending_for_car.setdefault(frame_id, {})
        frame_values.setdefault("_metadata", metadata or {
            "session_uid": session_uid,
            "vehicle_index": vehicle_index,
            "frame_id": frame_id,
        })
        frame_values[stream] = dict(values)
        pending_for_car.move_to_end(frame_id)

        if FRAME_JOIN_STREAMS.issubset(frame_values):
            joined = pending_for_car.pop(frame_id)
            if not pending_for_car:
                self.pending.pop(car_key)
            return joined

        while len(pending_for_car) > self.max_pending_frames:
            expired_frame_id, expired_values = pending_for_car.popitem(last=False)
            self.expired.append((car_key, expired_frame_id, expired_values))
        if not pending_for_car:
            self.pending.pop(car_key)
        return None

    def drain_expired(self):
        expired = self.expired
        self.expired = []
        return expired

    def flush(self):
        pending = self.drain_expired()
        for car_key, frames in self.pending.items():
            for frame_id, values in frames.items():
                pending.append((car_key, frame_id, values))
        self.pending.clear()
        return pending


def latest_with_frame_age(values, prefix, current_overall_frame_id):
    """Copy one latest-value cache entry and report its age in overall frames."""
    result = dict(values)
    source_overall_frame_id = result.pop("_overall_frame_id", None)
    age = None
    if source_overall_frame_id is not None and current_overall_frame_id is not None:
        age = max(0, current_overall_frame_id - source_overall_frame_id)
    if prefix in MAX_AGE_FRAMES:
        result[f"{prefix}_age_frames"] = age if age is not None else ""
        result[f"{prefix}_stale"] = (
            age is None or age > MAX_AGE_FRAMES[prefix]
        )
    elif age is not None:
        result[f"{prefix}_age_frames"] = age
    return result


def frame_join_metadata(meta, received_ns, damage=None, setup=None, session=None):
    return {
        "session_uid": meta.session_uid,
        "vehicle_index": meta.player_index,
        "packet_format": meta.packet_format,
        "session_time_s": meta.session_time_s,
        "frame_id": meta.frame_id,
        "overall_frame_id": meta.overall_frame_id,
        "received_ns": received_ns,
        "damage": dict(damage or {}),
        "setup": dict(setup or {}),
        "session": dict(session or {}),
    }


def mark_sample_completeness(sample, joined):
    missing = [stream for stream in ("lap", "motion", "status", "telemetry")
               if stream not in joined]
    sample["sample_complete"] = not missing
    sample["missing_streams"] = "|".join(missing)
    if missing:
        sample["training_eligible"] = False
    return sample


@dataclass(frozen=True)
class PacketMeta:
    packet_format: int
    packet_version: int
    packet_id: int
    session_uid: int
    session_time_s: float
    frame_id: int
    overall_frame_id: int
    player_index: int


def packet_meta(packet: bytes) -> Optional[PacketMeta]:
    if len(packet) < HEADER.size:
        return None
    values = HEADER.unpack_from(packet)
    return PacketMeta(
        packet_format=values[0],
        packet_version=values[4],
        packet_id=values[5],
        session_uid=values[6],
        session_time_s=values[7],
        frame_id=values[8],
        overall_frame_id=values[9],
        player_index=values[10],
    )


def valid_2026_packet(packet: bytes, packet_id: int) -> bool:
    meta = packet_meta(packet)
    return bool(
        meta
        and meta.packet_format == 2026
        and meta.packet_version == 1
        and meta.packet_id == packet_id
        and len(packet) == PACKET_SIZES_2026.get(packet_id)
    )


def valid_packet(packet: bytes, expected_format: int, packet_id: int) -> bool:
    meta = packet_meta(packet)
    if not meta or meta.packet_format != expected_format or meta.packet_id != packet_id:
        return False
    if meta.packet_version != 1:
        return False
    if expected_format == 2026:
        return len(packet) == PACKET_SIZES_2026.get(packet_id)
    if expected_format == 2025:
        return packet_id == 6 and len(packet) == 1352
    return False


def decode_session(packet: bytes) -> dict:
    weather_code, track_temp, air_temp, total_laps, track_length, session_type, track_id = (
        struct.unpack_from("<BbbBHBb", packet, 29)
    )
    safety_car_status = struct.unpack_from("<B", packet, 153)[0]
    sector2_start, sector3_start = struct.unpack_from("<ff", packet, 745)
    return {
        "weather_code": weather_code,
        "weather_label": WEATHER_LABELS.get(weather_code, "unknown"),
        "track_temp_c": track_temp,
        "air_temp_c": air_temp,
        "total_laps": total_laps,
        "track_length_m": track_length,
        "session_type_code": session_type,
        "track_id": track_id,
        "safety_car_status": safety_car_status,
        "sector2_start_m": sector2_start,
        "sector3_start_m": sector3_start,
    }


def _numeric_code(code):
    if code is None or code == "" or isinstance(code, bool):
        return None
    try:
        value = int(code)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isinstance(code, str) and value != code:
        return None
    return value


def classify_weather(code) -> str:
    value = _numeric_code(code)
    if value in (0, 1, 2):
        return "dry"
    if value in (3, 4, 5):
        return "wet"
    return "unknown"


def classify_compound(code) -> str:
    value = _numeric_code(code)
    if value in (16, 17, 18, 19, 20, 21, 22):
        return "dry"
    if value == 7:
        return "intermediate"
    if value == 8:
        return "wet"
    return "unknown"


def lap_condition_class(weather_classes, compound_classes) -> str:
    if weather_classes == {"dry"} and compound_classes == {"dry"}:
        return "dry"
    if (
        weather_classes == {"wet"}
        and compound_classes
        and compound_classes.issubset({"intermediate", "wet"})
    ):
        return "wet"
    return "unknown"


def decode_telemetry(packet: bytes, player_index: int) -> dict:
    values = TELEMETRY_CAR.unpack_from(
        packet, HEADER.size + player_index * TELEMETRY_CAR.size
    )
    return {
        "speed_kmh": values[0],
        "throttle_pct": values[1] * 100,
        "steering": values[2],
        "brake_pct": values[3] * 100,
        "clutch_pct": values[4],
        "gear": values[5],
        "rpm": values[6],
        "drs": values[7],
        "rev_lights_pct": values[8],
        "brake_temp_rl_c": values[10],
        "brake_temp_rr_c": values[11],
        "brake_temp_fl_c": values[12],
        "brake_temp_fr_c": values[13],
        "tyre_surface_temp_rl_c": values[14],
        "tyre_surface_temp_rr_c": values[15],
        "tyre_surface_temp_fl_c": values[16],
        "tyre_surface_temp_fr_c": values[17],
        "tyre_inner_temp_rl_c": values[18],
        "tyre_inner_temp_rr_c": values[19],
        "tyre_inner_temp_fl_c": values[20],
        "tyre_inner_temp_fr_c": values[21],
        "engine_temp_c": values[22],
        "tyre_pressure_rl_psi": values[23],
        "tyre_pressure_rr_psi": values[24],
        "tyre_pressure_fl_psi": values[25],
        "tyre_pressure_fr_psi": values[26],
        "surface_type_rl": values[27],
        "surface_type_rr": values[28],
        "surface_type_fl": values[29],
        "surface_type_fr": values[30],
    }


def decode_legacy_telemetry(packet: bytes, player_index: int) -> dict:
    values = LEGACY_TELEMETRY_CAR.unpack_from(
        packet, HEADER.size + player_index * 60
    )
    return {
        "speed_kmh": values[0],
        "throttle_pct": values[1] * 100,
        "steering": values[2],
        "brake_pct": values[3] * 100,
        "gear": values[5],
        "rpm": values[6],
    }


def decode_car_setup(packet: bytes, player_index: int) -> dict:
    values = CAR_SETUP.unpack_from(
        packet, HEADER.size + player_index * CAR_SETUP.size
    )
    setup = dict(zip(CAR_SETUP_VALUE_FIELDS, values))
    next_wing_offset = HEADER.size + 24 * CAR_SETUP.size
    setup["next_front_wing_value"] = struct.unpack_from(
        "<f", packet, next_wing_offset
    )[0]
    return setup


def decode_motion(packet: bytes, player_index: int) -> dict:
    values = MOTION_CAR.unpack_from(
        packet, HEADER.size + player_index * MOTION_CAR.size
    )
    return {
        "world_x_m": values[0],
        "world_y_m": values[1],
        "world_z_m": values[2],
        "velocity_x_mps": values[3],
        "velocity_y_mps": values[4],
        "velocity_z_mps": values[5],
        "yaw_rad": values[15],
        "pitch_rad": values[16],
        "roll_rad": values[17],
    }


def decode_lap(packet: bytes, player_index: int) -> dict:
    values = LAP_CAR.unpack_from(
        packet, HEADER.size + player_index * LAP_CAR.size
    )
    return {
        "last_lap_time_s": values[0] / 1000,
        "current_lap_time_s": values[1] / 1000,
        "lap_distance_m": values[10],
        "total_distance_m": values[11],
        "position": values[13],
        "lap_number": values[14],
        "pit_status": values[15],
        "sector": values[17],
        "lap_invalid": values[18],
        "driver_status": values[27],
    }


def decode_flashback(packet: bytes) -> dict:
    event_code, frame_id, session_time_s = FLASHBACK_EVENT.unpack_from(
        packet, HEADER.size
    )
    return {
        "event_code": event_code.decode("ascii", "replace"),
        "flashback_frame_id": frame_id,
        "flashback_session_time_s": session_time_s,
    }


def decode_damage(packet: bytes, player_index: int) -> dict:
    offset = HEADER.size + player_index * 46
    values = struct.unpack_from("<4f12B", packet, offset)
    result = {}
    for wheel, index in (("rl", 0), ("rr", 1), ("fl", 2), ("fr", 3)):
        result[f"tyre_wear_{wheel}_pct"] = values[index]
        result[f"tyre_damage_{wheel}_pct"] = values[4 + index]
        result[f"brake_damage_{wheel}_pct"] = values[8 + index]
        result[f"tyre_blistering_{wheel}_pct"] = values[12 + index]
    return result


def decode_status(packet: bytes, player_index: int) -> dict:
    offset = HEADER.size + player_index * 59
    values = struct.unpack_from("<5B3f2H2BH3B", packet, offset)
    return {
        "tyre_compound_actual": values[13],
        "tyre_compound_visual": values[14],
        "tyre_age_laps": values[15],
    }