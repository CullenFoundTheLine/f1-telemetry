"""Capture F1 25 / F1 25: 2026 Season Pack telemetry from PS5 over UDP.

Writes validated player-car samples to a CSV compatible with telemetry_graph.py.
Only Car Telemetry packets (packet ID 6) are sampled. A new CSV is started
on session changes or backwards session timestamps (e.g. flashbacks).

Run: python3 f1_25_udp.py --seconds 60
Stop at any time with Ctrl+C. PS5 and Mac must be on a reachable LAN.
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

# Official EA UDP structures are little-endian and packed, with 29-byte headers.
HEADER = struct.Struct("<HBBBBBQfIIBB")
CAR_PREFIX = struct.Struct("<HfffBbHBBH")  # speed, throttle, steer, brake, clutch, gear, RPM, DRS, lights, light bits
PACKET_LAYOUT = {
    2025: (22, 60, 1352),
    2026: (24, 59, 1448),
}
CSV_COLUMNS = ("time_s", "speed_kmh", "throttle", "brake", "steer", "gear", "rpm")


class InvalidPacket(ValueError):
    """Binary data does not match the expected F1 packet layout."""


def decode_player_telemetry(packet: bytes) -> dict | None:
    """Decode one player-car Car Telemetry packet, or return None for other IDs.

    Raises InvalidPacket on malformed/unsupported car telemetry packets.
    Does not attempt to infer identity, team, lap number, or driving mode.
    """
    if len(packet) < HEADER.size:
        raise InvalidPacket(f"short header: {len(packet)} bytes")

    (packet_format, game_year, major, minor, version, packet_id,
     session_uid, session_time, frame, overall_frame, player_index,
     secondary_index) = HEADER.unpack_from(packet)

    if packet_id != 6:  # Other packets contain different data; ignore them.
        return None
    if packet_format not in PACKET_LAYOUT:
        raise InvalidPacket(f"unsupported UDP format {packet_format}; use 2025 or 2026")
    if version != 1:
        raise InvalidPacket(f"unsupported car telemetry packet version {version}")

    num_cars, car_size, packet_size = PACKET_LAYOUT[packet_format]
    if len(packet) != packet_size:
        raise InvalidPacket(
            f"format {packet_format}: expected {packet_size} bytes, received {len(packet)}"
        )
    if not (0 <= player_index < num_cars):
        raise InvalidPacket(f"player car index {player_index} out of range")
    if not (math.isfinite(session_time) and session_time >= 0):
        raise InvalidPacket("nonfinite or negative game session timestamp")

    offset = HEADER.size + player_index * car_size
    (speed, throttle, steer, brake, clutch, gear, rpm, drs,
     rev_lights_percent, rev_lights_bits) = CAR_PREFIX.unpack_from(packet, offset)
    if not (0 <= speed <= 500):
        raise InvalidPacket(f"implausible car speed {speed} km/h")
    if not (math.isfinite(throttle) and 0 <= throttle <= 1):
        raise InvalidPacket(f"invalid throttle {throttle}")
    if not (math.isfinite(brake) and 0 <= brake <= 1):
        raise InvalidPacket(f"invalid brake {brake}")
    if not (math.isfinite(steer) and -1 <= steer <= 1):
        raise InvalidPacket(f"invalid steering {steer}")
    if not (-1 <= gear <= 8 and 0 <= rpm <= 30000):
        raise InvalidPacket(f"invalid gear {gear} / RPM {rpm}")

    return {
        "format": packet_format,
        "session_uid": session_uid,
        "session_time": session_time,
        "frame": frame,
        "overall_frame": overall_frame,
        "player_index": player_index,
        "speed_kmh": speed,
        "throttle": throttle,
        "brake": brake,
        "steer": steer,
        "gear": gear,
        "rpm": rpm,
    }


def save_graph(csv_path: Path) -> None:
    """Reuse the existing CSV validator and grapher; no silent fake graph."""
    try:
        import matplotlib
        matplotlib.use("Agg")  # save on macOS/headless systems without needing a GUI
        import telemetry_graph
        times, speeds = telemetry_graph.load_telemetry(csv_path)
        png = csv_path.with_name(csv_path.stem + "_speed_vs_time.png")
        telemetry_graph.save_graph(times, speeds, png, show=False)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        print(f"Graph not created for {csv_path.name}: {exc}", file=sys.stderr)
        print("Your CSV is retained. Put telemetry_graph.py beside this script and install matplotlib.", file=sys.stderr)


class SessionWriter:
    """Separate segments so the original CSV validator never receives time reversals."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.file = None
        self.writer = None
        self.path: Path | None = None
        self.session_uid: int | None = None
        self.start_time: float | None = None
        self.last_time: float | None = None
        self.last_frame: int | None = None
        self.last_overall_frame: int | None = None
        self.packet_format: int | None = None
        self.rows = 0
        self.segment = 0
        self.paths: list[Path] = []
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    def _start(self, sample: dict) -> None:
        self.segment += 1
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.out_dir / (
            f"f1_{sample['format']}_{self.run_id}_part{self.segment:02d}.csv"
        )
        self.file = self.path.open("x", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.file, fieldnames=CSV_COLUMNS)
        self.writer.writeheader()
        self.session_uid = sample["session_uid"]
        self.packet_format = sample["format"]
        self.start_time = sample["session_time"]
        self.last_time = None
        self.last_frame = None
        self.last_overall_frame = None
        self.rows = 0
        print(f"Recording player car (index {sample['player_index']}) to {self.path}")

    def append(self, sample: dict) -> bool:
        t = sample["session_time"]
        if self.file is not None and sample["session_uid"] == self.session_uid:
            # Old datagrams may arrive out of order. Do not mistake them for a flashback.
            if (self.last_overall_frame is not None
                    and sample["overall_frame"] <= self.last_overall_frame):
                return False
        if self.file is not None and (
            sample["session_uid"] != self.session_uid
            or sample["format"] != self.packet_format
            or (self.last_time is not None and t < self.last_time)
        ):
            print("Session changed or game time moved backwards; starting a separate CSV.")
            self.close_segment()

        if self.file is None:
            self._start(sample)

        # Pauses/duplicate datagrams/reordered older frames must not create duplicate timestamps.
        if self.last_time is not None and t == self.last_time:
            return False
        # In a monotonic session a duplicate frame is also a duplicate sample.
        if self.last_frame is not None and sample["frame"] == self.last_frame:
            return False

        self.writer.writerow({
            "time_s": f"{t - self.start_time:.6f}",
            "speed_kmh": sample["speed_kmh"],
            "throttle": f"{sample['throttle']:.6f}",
            "brake": f"{sample['brake']:.6f}",
            "steer": f"{sample['steer']:.6f}",
            "gear": sample["gear"],
            "rpm": sample["rpm"],
        })
        self.rows += 1
        self.last_time = t
        self.last_frame = sample["frame"]
        self.last_overall_frame = sample["overall_frame"]
        self.file.flush()  # retain completed rows if the program is interrupted
        return True

    def close_segment(self) -> None:
        if self.file is None:
            return
        self.file.close()
        if self.rows >= 2:
            self.paths.append(self.path)
            print(f"Saved {self.rows} samples: {self.path}")
        else:
            print(f"Only {self.rows} sample; graph requires at least two: {self.path}")
        self.file = None
        self.writer = None
        self.last_time = None
        self.last_frame = None
        self.last_overall_frame = None

    def close(self) -> list[Path]:
        self.close_segment()
        return self.paths


def capture(port: int, seconds: float, out_dir: Path, make_graphs: bool) -> int:
    writer = SessionWriter(out_dir)
    all_packets = valid = invalid = 0
    saw_udp = False
    last_report = time.monotonic()
    deadline = time.monotonic() + seconds if seconds else None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.settimeout(1.0)
            print(f"Listening for PS5 UDP on 0.0.0.0:{port} (Ctrl+C to stop)")
            print("Waiting for telemetry; enter an on-track driving session on PS5.")
            while deadline is None or time.monotonic() < deadline:
                try:
                    packet, sender = sock.recvfrom(65535)
                except socket.timeout:
                    if time.monotonic() - last_report >= 5:
                        print("Waiting for UDP packets... check PS5 target IP, port and network.")
                        last_report = time.monotonic()
                    continue

                all_packets += 1
                if not saw_udp:
                    print(f"First UDP datagram from {sender[0]}:{sender[1]} ({len(packet)} bytes).")
                    saw_udp = True
                try:
                    sample = decode_player_telemetry(packet)
                    if sample is None:
                        continue
                    if writer.append(sample):
                        valid += 1
                        if time.monotonic() - last_report >= 1:
                            print(f"LIVE | format {sample['format']} | "
                                  f"speed {sample['speed_kmh']} km/h | "
                                  f"throttle {sample['throttle']:.0%} | "
                                  f"brake {sample['brake']:.0%} | "
                                  f"samples {valid}")
                            last_report = time.monotonic()
                except InvalidPacket as exc:
                    invalid += 1
                    if invalid <= 5:
                        print(f"Ignored invalid telemetry packet: {exc}")
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except OSError as exc:
        print(f"Socket / file error: {exc}", file=sys.stderr)
        return 1
    finally:
        paths = writer.close()
        if make_graphs:
            for path in paths:
                save_graph(path)
        print(f"UDP datagrams: {all_packets}; valid recorded samples: {valid}; "
              f"invalid telemetry datagrams: {invalid}; graphable segments: {len(paths)}")
        if all_packets and not valid:
            print("UDP arrived but no valid player-car telemetry. Check UDP Format and drive on track.")
        elif not all_packets:
            print("No UDP received. Replace PS5 127.0.0.1 with your Mac's local IPv4 address.")
    return 0 if valid >= 2 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=20777, help="UDP port (default: 20777)")
    parser.add_argument("--seconds", type=float, default=0,
                        help="Capture duration in real seconds; default: until Ctrl+C")
    parser.add_argument("--output-dir", type=Path, default=Path("recordings"),
                        help="Where CSV/PNG outputs go (default: recordings)")
    parser.add_argument("--no-graph", action="store_true", help="Capture CSV without plotting")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error("port must be 1..65535 and seconds must be a finite nonnegative number")
    return capture(args.port, args.seconds, args.output_dir, not args.no_graph)


if __name__ == "__main__":
    sys.exit(main())
