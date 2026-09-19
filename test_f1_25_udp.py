"""Synthetic protocol and loopback tests; does not claim to exercise a real PS5."""
import csv
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import f1_25_udp as f1


def packet(fmt=2026, seconds=12.0, frame=1, overall=None, uid=42,
           player=3, speed=200, packet_id=6, bad_size=False):
    n, stride, expected = f1.PACKET_LAYOUT[fmt]
    header = f1.HEADER.pack(fmt, fmt - 2000, 1, 0, 1, packet_id, uid,
                            seconds, frame, frame if overall is None else overall,
                            player, 255)
    buf = bytearray(expected)
    buf[:len(header)] = header
    car = f1.CAR_PREFIX.pack(speed, 0.75, -0.2, 0.3, 0, 5, 11500, 0, 22, 0)
    buf[f1.HEADER.size + player * stride: f1.HEADER.size + player * stride + len(car)] = car
    return bytes(buf[:-1] if bad_size else buf)


class F1PacketTests(unittest.TestCase):
    def test_protocol_2025_and_2026_player_not_car_zero(self):
        for fmt in (2025, 2026):
            sample = f1.decode_player_telemetry(packet(fmt=fmt))
            self.assertEqual(sample["speed_kmh"], 200)
            self.assertEqual(sample["player_index"], 3)
            self.assertAlmostEqual(sample["throttle"], 0.75)
            self.assertEqual(sample["format"], fmt)

    def test_ignore_other_packets_and_reject_malformed(self):
        self.assertIsNone(f1.decode_player_telemetry(packet(packet_id=2)))
        for data in (b"oops", packet(bad_size=True)):
            with self.assertRaises(f1.InvalidPacket):
                f1.decode_player_telemetry(data)

    def test_split_flashback_but_drop_out_of_order(self):
        with tempfile.TemporaryDirectory() as temp:
            writer = f1.SessionWriter(Path(temp))
            for t, frame, overall in ((10, 10, 10), (10.1, 11, 11),
                                      (10.05, 10, 10), # reordered; drop
                                      (9, 6, 12), (9.1, 7, 13)):
                sample = f1.decode_player_telemetry(packet(seconds=t, frame=frame,
                                                            overall=overall))
                writer.append(sample)
            paths = writer.close()
            self.assertEqual(len(paths), 2)
            for path in paths:
                with path.open(newline="") as file:
                    rows = list(csv.DictReader(file))
                self.assertEqual(len(rows), 2)
                self.assertEqual(float(rows[0]["time_s"]), 0)

    def test_udp_receive_to_csv_and_real_graph(self):
        with tempfile.TemporaryDirectory() as temp:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            result = []
            t = threading.Thread(target=lambda: result.append(
                f1.capture(port, 1.1, Path(temp), True)), daemon=True)
            t.start()
            time.sleep(0.15)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                for idx, speed in enumerate((130, 148, 169, 185)):
                    sender.sendto(packet(seconds=30 + idx * 0.05,
                                         frame=idx + 1, speed=speed),
                                  ("127.0.0.1", port))
                    time.sleep(0.035)
            t.join(timeout=4)
            self.assertFalse(t.is_alive())
            self.assertEqual(result, [0])
            paths = list(Path(temp).glob("*.csv"))
            self.assertEqual(len(paths), 1)
            import telemetry_graph
            times, speeds = telemetry_graph.load_telemetry(paths[0])
            self.assertEqual(speeds, [130, 148, 169, 185])
            self.assertEqual(len(times), 4)
            self.assertEqual(len(list(Path(temp).glob("*.png"))), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
