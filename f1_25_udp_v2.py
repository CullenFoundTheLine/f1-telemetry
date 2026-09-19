"""F1 25 2026 Season Pack PS5 UDP: preserve raw data, classify pit vs track,
write on-track stints, game-reported pit timing, lap/position/gap metadata.

Uses the tested f1_25_udp.py car telemetry decoder and telemetry_graph.py.
Run: python3 f1_25_udp_v2.py --seconds 180
"""
from __future__ import annotations

import argparse
import csv
import math
import socket
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

from f1_25_udp import HEADER, InvalidPacket, decode_player_telemetry, save_graph

# EA 2026 Season Pack packed, little endian specification.
FORMAT = 2026
LAP_SIZE = 1399
LAP_CAR_STRIDE = 57
MOTION_SIZE = 1325
MOTION_CAR_STRIDE = 54
SESSION_SIZE = 926
COLS = (
    "time_s", "session_time_s", "speed_kmh", "throttle", "brake", "steer", "gear", "rpm",
    "status", "lap_number", "lap_distance_m", "total_distance_m", "current_lap_time_s",
    "last_lap_time_s", "lap_invalid", "sector", "race_position", "grid_position",
    "pit_status", "driver_status", "pit_lane_timer_active", "pit_lane_elapsed_s",
    "pit_stop_elapsed_s", "gap_ahead_s", "gap_behind_s", "gap_to_leader_s",
    "car_ahead_index", "car_behind_index", "world_x_m", "world_y_m", "world_z_m",
    "track_length_m", "pit_speed_limit_kmh", "session_type",
)
EVENT_COLS = (
    "event", "session_time_s", "elapsed_since_recording_s", "observed_garage_to_exit_s",
    "observed_pit_lane_s", "game_pit_lane_timer_s", "game_pit_stop_timer_s", "note"
)


def header(packet: bytes) -> dict:
    if len(packet) < HEADER.size:
        raise InvalidPacket(f"short header {len(packet)}")
    (fmt, _, _, _, version, packet_id, uid, stime, frame, overall, player, _) = HEADER.unpack_from(packet)
    if fmt != FORMAT:
        raise InvalidPacket(f"Expected F1 25 UDP Format 2026, got {fmt}; change the PS5 UDP Format")
    if version != 1:
        raise InvalidPacket(f"Unexpected packet version {version} for packet {packet_id}")
    if not math.isfinite(stime) or stime < 0 or player >= 24:
        raise InvalidPacket("invalid time or player index")
    return {"id": packet_id, "uid": uid, "time": stime, "frame": frame,
            "overall": overall, "player": player}


def lap_car(packet: bytes, i: int) -> dict:
    offset = HEADER.size + LAP_CAR_STRIDE * i
    u8 = lambda n: packet[offset + n]
    u16 = lambda n: struct.unpack_from("<H", packet, offset + n)[0]
    u32 = lambda n: struct.unpack_from("<I", packet, offset + n)[0]
    f32 = lambda n: struct.unpack_from("<f", packet, offset + n)[0]
    distance, total = f32(20), f32(24)
    if not math.isfinite(distance) or not math.isfinite(total) or abs(distance) > 100000 or abs(total) > 100000000:
        raise InvalidPacket("invalid lap distance")
    position, lap_num, pit, driver, result = u8(32), u8(33), u8(34), u8(44), u8(45)
    if not (pit <= 2 and driver <= 4 and result <= 7):
        raise InvalidPacket("invalid pit, driver or result status")

    def gap(ms_pos: int, minutes_pos: int) -> float | None:
        ms, minutes = u16(ms_pos), u8(minutes_pos)
        if ms >= 60000 or minutes == 255:
            return None
        value = (minutes * 60000 + ms) / 1000
        return value if value > 0 else None  # zero is often an unavailable delta

    return {
        "car_index": i, "position": position or None, "lap_number": lap_num or None,
        "lap_distance_m": round(distance, 3), "total_distance_m": round(total, 3),
        "current_lap_time_s": u32(4) / 1000, "last_lap_time_s": u32(0) / 1000 if u32(0) else None,
        "gap_ahead_s": gap(14, 16), "gap_to_leader_s": gap(17, 19),
        "pit_status": pit, "driver_status": driver, "result_status": result,
        "sector": u8(36) + 1 if u8(36) <= 2 else None,
        "lap_invalid": u8(37), "grid_position": u8(43) or None,
        "pit_lane_timer_active": u8(46), "pit_lane_elapsed_s": u16(47) / 1000,
        "pit_stop_elapsed_s": u16(49) / 1000,
    }


def decode_lap(packet: bytes, h: dict) -> dict:
    if len(packet) != LAP_SIZE:
        raise InvalidPacket(f"2026 Lap Data length must be {LAP_SIZE}, got {len(packet)}")
    cars = [lap_car(packet, i) for i in range(24)]
    player = cars[h["player"]]
    if player["pit_lane_timer_active"] not in (0, 1):
        raise InvalidPacket("invalid pit-lane timer flag")
    # Race-position deltas are from Lap Data; only show same-lap, active rival info.
    ahead = behind = None
    if player["position"] and player["result_status"] == 2:
        for car in cars:
            if car["car_index"] == h["player"] or car["result_status"] != 2:
                continue
            if not player["lap_number"] or car["lap_number"] != player["lap_number"]:
                continue
            if car["position"] == player["position"] - 1:
                ahead = car
            if car["position"] == player["position"] + 1:
                behind = car
    player["car_ahead_index"] = ahead["car_index"] if ahead else None
    player["car_behind_index"] = behind["car_index"] if behind else None
    player["gap_ahead_s"] = player["gap_ahead_s"] if ahead else None
    player["gap_behind_s"] = behind["gap_ahead_s"] if behind else None
    leader = next((c for c in cars if c["position"] == 1 and c["result_status"] == 2), None)
    player["gap_to_leader_s"] = (
        0.0 if player["position"] == 1 and player["result_status"] == 2 else
        player["gap_to_leader_s"] if leader and player["result_status"] == 2
        and leader["lap_number"] == player["lap_number"] else None
    )
    return {"header": h, "player": player}


def decode_motion(packet: bytes, h: dict) -> dict:
    if len(packet) != MOTION_SIZE:
        raise InvalidPacket(f"2026 Motion length must be {MOTION_SIZE}, got {len(packet)}")
    xyz = struct.unpack_from("<fff", packet, HEADER.size + h["player"] * MOTION_CAR_STRIDE)
    if not all(math.isfinite(v) and abs(v) < 1000000 for v in xyz):
        raise InvalidPacket("invalid world coordinates")
    return {"header": h, "xyz": xyz}


def decode_session(packet: bytes, h: dict) -> dict:
    if len(packet) != SESSION_SIZE:
        raise InvalidPacket(f"2026 Session length must be {SESSION_SIZE}, got {len(packet)}")
    length = struct.unpack_from("<H", packet, HEADER.size + 4)[0]
    return {"header": h, "track_length_m": length or None,
            "session_type": packet[HEADER.size + 6],
            "pit_speed_limit_kmh": packet[HEADER.size + 13] or None}


def same_frame(snapshot: dict | None, h: dict) -> bool:
    return bool(snapshot and snapshot["header"]["uid"] == h["uid"]
                and snapshot["header"]["overall"] == h["overall"]
                and snapshot["header"]["player"] == h["player"])


class Recorder:
    def __init__(self, out_dir: Path, graph: bool = True):
        self.out_dir = out_dir
        self.graph = graph
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.part = 0
        self.uid = None
        self.last_time = None
        self.last_overall = None
        self.last_lap = None
        self.last_motion = None
        self.last_session = None
        self.raw_file = self.raw_writer = self.events_file = self.events_writer = None
        self.stint_file = self.stint_writer = None
        self.stint_start = None
        self.stint_rows = 0
        self.stint_index = 0
        self.stints = []
        self.first_t = None
        self.first_pit_t = None
        self.lane_entry_t = None
        self.last_lane_timer = None
        self.last_stop_timer = None
        self.last_state = None
        self.rows = 0

    def _event(self, event: str, t: float, *, garage=None, lane=None, game_lane=None, stop=None, note=""):
        assert self.events_writer is not None
        row = dict(zip(EVENT_COLS, (event, round(t, 6), round(t - self.first_t, 6),
                                     garage, lane, game_lane, stop, note)))
        self.events_writer.writerow(row)
        self.events_file.flush()
        print(f"EVENT | {event} at session {t:.3f}s | {note}")

    def _start_part(self, sample: dict):
        self.part += 1
        self.out_dir.mkdir(parents=True, exist_ok=True)
        base = f"f1_{FORMAT}_{self.run_id}_part{self.part:02d}"
        self.base = self.out_dir / base
        self.raw_file = self.base.with_name(base + "_all.csv").open("x", newline="", encoding="utf-8")
        self.raw_writer = csv.DictWriter(self.raw_file, COLS)
        self.raw_writer.writeheader()
        self.events_file = self.base.with_name(base + "_events.csv").open("x", newline="", encoding="utf-8")
        self.events_writer = csv.DictWriter(self.events_file, EVENT_COLS)
        self.events_writer.writeheader()
        self.first_t = sample["session_time"]
        self.last_time = None
        self.last_overall = None
        self.last_state = None
        self.first_pit_t = None
        self.lane_entry_t = None
        self.last_lane_timer = None
        self.last_stop_timer = None
        self.stint_index = 0
        self.rows = 0
        print("RAW (never trimmed):", self.raw_file.name)

    def _close_stint(self):
        if self.stint_file is None:
            return
        self.stint_file.close()
        if self.stint_rows >= 2:
            self.stints.append(self.stint_path)
        else:
            print("Stint contained fewer than two samples:", self.stint_path)
        self.stint_file = self.stint_writer = None
        self.stint_start = None
        self.stint_rows = 0

    def _start_stint(self, t):
        self.stint_index += 1
        self.stint_start = t
        self.stint_path = self.base.with_name(self.base.name + f"_ontrack_stint{self.stint_index:02d}.csv")
        self.stint_file = self.stint_path.open("x", newline="", encoding="utf-8")
        self.stint_writer = csv.DictWriter(self.stint_file, COLS)
        self.stint_writer.writeheader()
        print("ON-TRACK:", self.stint_path.name)

    def _close_part(self):
        self._close_stint()
        if self.raw_file is not None:
            self.raw_file.close()
            self.events_file.close()
            print(f"Saved {self.rows} original-sample rows and pit events: {self.base.name}")
        self.raw_file = self.raw_writer = self.events_file = self.events_writer = None

    def close(self):
        self._close_part()
        # Plot only once capture has stopped: plotting mid-pit would block UDP reception.
        if self.graph:
            for path in self.stints:
                save_graph(path)

    def process(self, packet: bytes) -> bool:
        """Returns true only when a validated telemetry sample was recorded."""
        h = header(packet)
        if h["id"] not in (0, 1, 2, 6):
            return False
        if self.uid is not None and h["uid"] != self.uid:
            self._close_part()
            self._reset_caches()
        if self.uid is None:
            self.uid = h["uid"]
        # Overall frame increases on flashback, game time rewinds: new part.
        if (self.last_time is not None and h["overall"] > self.last_overall
                and h["time"] < self.last_time - 0.02):
            self._close_part()
            self._reset_caches()
            self.uid = h["uid"]
        if h["id"] == 2:
            self.last_lap = decode_lap(packet, h)
            return False
        if h["id"] == 0:
            self.last_motion = decode_motion(packet, h)
            return False
        if h["id"] == 1:
            self.last_session = decode_session(packet, h)
            return False
        sample = decode_player_telemetry(packet)
        if sample is None:
            return False
        if self.last_overall is not None and h["overall"] <= self.last_overall:
            return False
        if self.last_time is not None and h["time"] <= self.last_time:
            return False
        if self.raw_file is None:
            self._start_part(sample)
        lap = self.last_lap["player"] if same_frame(self.last_lap, h) else None
        motion = self.last_motion["xyz"] if same_frame(self.last_motion, h) else None
        sess = (self.last_session if self.last_session and
                self.last_session["header"]["uid"] == h["uid"] else None)
        # No speed-limit heuristic in live capture. Unknown when lap data unavailable.
        state = ("pit_or_garage" if lap and (lap["pit_status"] in (1, 2)
                 or lap["pit_lane_timer_active"] == 1 or lap["driver_status"] == 0)
                 else "on_track" if lap and lap["pit_status"] == 0
                 and lap["pit_lane_timer_active"] == 0
                 and lap["driver_status"] in (1, 2, 3, 4)
                 else "unknown")
        t = sample["session_time"]
        record = {k: "" for k in COLS}
        record.update({"time_s": f"{t - self.first_t:.2f}", "session_time_s": f"{t:.2f}",
                   "speed_kmh": f"{sample['speed_kmh']:.0f}", "throttle": f"{sample['throttle']:.2f}",
                   "brake": f"{sample['brake']:.2f}", "steer": f"{sample['steer']:.2f}",
                   "gear": f"{sample['gear']:.0f}", "rpm": f"{sample['rpm']:.0f}", "status": state})
        if lap:
            renamed = {"position": "race_position", "gap_ahead_s": "gap_ahead_s",
                       "gap_behind_s": "gap_behind_s", "gap_to_leader_s": "gap_to_leader_s"}
            for k, v in lap.items():
                key = renamed.get(k, k)
                if key in record and v is not None:
                    record[key] = v
        if motion:
            record.update(zip(("world_x_m", "world_y_m", "world_z_m"), (round(v, 3) for v in motion)))
        if sess:
            for k in ("track_length_m", "session_type", "pit_speed_limit_kmh"):
                if sess[k] is not None:
                    record[k] = sess[k]
        self.raw_writer.writerow(record)
        self.raw_file.flush()
        self.rows += 1
        self.last_time, self.last_overall = t, h["overall"]

        if lap and lap["pit_lane_timer_active"]:
            if self.lane_entry_t is None:
                self.lane_entry_t = t
                self._event("pit_lane_observed", t, note="First observed active in-game pit-lane timer; may be partial")
            self.last_lane_timer = max(self.last_lane_timer or 0, lap["pit_lane_elapsed_s"])
            self.last_stop_timer = max(self.last_stop_timer or 0, lap["pit_stop_elapsed_s"])
        if state == "pit_or_garage":
            if self.first_pit_t is None:
                self.first_pit_t = t
            if self.last_state == "on_track":
                self._event("pit_entry_observed", t, note="First on-track -> pit/garage status transition")
            self._close_stint()
        elif state == "on_track":
            if self.stint_file is None:
                if self.first_pit_t is not None:
                    garage = t - self.first_pit_t
                    lane = t - self.lane_entry_t if self.lane_entry_t is not None else None
                    self._event("pit_exit_observed", t, garage=round(garage, 3),
                                lane=round(lane, 3) if lane is not None else None,
                                game_lane=self.last_lane_timer, stop=self.last_stop_timer,
                                note="Garage-to-exit counts observed wait; official lane timer if present")
                else:
                    self._event("on_track_first_observed", t,
                                note="No preceding pit state captured; do not infer pit duration")
                self._start_stint(t)
                self.first_pit_t = self.lane_entry_t = self.last_lane_timer = self.last_stop_timer = None
            out = dict(record)
            out["time_s"] = f"{t - self.stint_start:.6f}"
            self.stint_writer.writerow(out)
            self.stint_file.flush()
            self.stint_rows += 1
        # Unknown/missing Lap Data doesn't terminate a stint or invent a status.
        if state != "unknown":
            self.last_state = state
        return True

    def _reset_caches(self):
        self.uid = self.last_time = self.last_overall = None
        self.last_lap = self.last_motion = self.last_session = None


def capture(port: int, seconds: float, output: Path, graph: bool) -> int:
    recorder = Recorder(output, graph)
    received = samples = bad = laps = 0
    deadline = time.monotonic() + seconds if seconds else None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("0.0.0.0", port))
            sock.settimeout(0.5)
            print(f"Listening for F1 25 UDP Format 2026 on 0.0.0.0:{port}")
            print("Drive on PS5. Stop with Ctrl+C. Raw and on-track CSVs are separate.")
            while deadline is None or time.monotonic() < deadline:
                try:
                    packet, address = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                received += 1
                if received == 1:
                    print("First datagram received from", address)
                try:
                    if recorder.process(packet):
                        samples += 1
                        if samples % 120 == 0:
                            print(f"LIVE | samples {samples} | lap packets {laps} | raw and on-track recording")
                    if len(packet) >= HEADER.size and packet[6] == 2:
                        laps += 1
                except InvalidPacket as exc:
                    bad += 1
                    if bad <= 8:
                        print("Rejected malformed/unsupported packet:", exc, file=sys.stderr)
    except KeyboardInterrupt:
        print("Stopped by user.")
    except OSError as exc:
        print("Socket/file error:", exc, file=sys.stderr)
        return 1
    finally:
        recorder.close()
        print(f"UDP datagrams {received}; recorded samples {samples}; rejected packets {bad}; "
              f"on-track stints {len(recorder.stints)}")
        if samples and not recorder.stints:
            print("No verified on-track stint. Check 2026 UDP format and Lap Data packet delivery.")
    return 0 if samples else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=20777)
    parser.add_argument("--seconds", type=float, default=0)
    parser.add_argument("--output-dir", type=Path, default=Path("recordings"))
    parser.add_argument("--no-graph", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error("Invalid port or duration")
    return capture(args.port, args.seconds, args.output_dir, not args.no_graph)


if __name__ == "__main__":
    sys.exit(main())
