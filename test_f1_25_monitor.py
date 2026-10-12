"""Synthetic 2026-packet tests; a real PS5 run still must confirm live fields."""
import csv
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

import f1_25_monitor as monitor
import f1_25_udp as old
import f1_25_udp_v2 as v2
import tyre_compare


def make_packet(packet_id, *, t=10.0, frame=1, player=21, uid=44):
    size = {0: 1325, 1: 926, 2: 1399, 4: 1470, 6: 1448,
            7: 1445, 11: 1460, 12: 231}[packet_id]
    data = bytearray(size)
    data[:old.HEADER.size] = old.HEADER.pack(2026, 26, 1, 0, 1, packet_id,
                                             uid, t, frame, frame, player, 255)
    return data


def participants():
    data = make_packet(4)
    data[29] = 22
    for idx, name, team_id in ((21, 'Cullen Louis', 8), (3, 'Other Driver', 12)):
        start = 30 + idx * 60
        data[start+10:start+10+len(name)] = name.encode()
        struct.pack_into('<H', data, start+5, team_id)
    return bytes(data)


def lap(*, t=10., frame=1, player=21):
    data = make_packet(2, t=t, frame=frame, player=player)
    for idx, position in ((21, 2), (3, 1)):
        offset = 29 + idx * 57
        struct.pack_into('<I', data, offset, 81500)
        struct.pack_into('<I', data, offset+4, 20000)
        struct.pack_into('<f', data, offset+20, 1000.)
        struct.pack_into('<f', data, offset+24, 6250.)
        for pos, val in ((32, position), (33, 4), (34, 0), (44, 4), (45, 2)):
            data[offset+pos] = val
    return bytes(data)


def history(idx, laps, tyre_stints, *, t=10.5):
    data = make_packet(11, t=t, player=21)
    data[29] = idx
    data[30] = len(laps)
    data[31] = len(tyre_stints)
    for j, (seconds, valid) in enumerate(laps):
        offset = 29 + 7 + 14*j
        struct.pack_into('<I', data, offset, round(seconds*1000))
        data[offset + 13] = 1 if valid else 0
    tyre_start = 29 + 7 + 100*14
    for j, triple in enumerate(tyre_stints):
        data[tyre_start + 3*j:tyre_start+3*j+3] = bytes(triple)
    return bytes(data)


def status(*, t=10.0, visual=18, actual=20, age=3):
    data = make_packet(7, t=t)
    start = 29 + 21*59
    data[start+25:start+28] = bytes((actual, visual, age))
    rival = 29 + 3*59
    data[rival+25:rival+28] = bytes((18, 16, 2))  # Rival on Soft C3
    return bytes(data)


def tyre_sets(*, t=10.05, wear=15):
    data = make_packet(12, t=t)
    data[29] = 21
    data[230] = 4  # fitted set index
    start = 30 + 4*10
    data[start+2] = wear
    data[start+5] = 16
    data[start+9] = 1
    return bytes(data)


def telemetry(*, t=11., frame=2):
    data = make_packet(6, t=t, frame=frame)
    start = 29 + 21*59
    car = old.CAR_PREFIX.pack(240, .9, .1, 0., 0, 6, 11800, 0, 50, 0)
    data[start:start+len(car)] = car
    return bytes(data)


def rows(path):
    with path.open(newline='', encoding='utf-8') as fh:
        return list(csv.DictReader(fh))


class MonitorTests(unittest.TestCase):
    def test_participant_names_and_status_compounds(self):
        people = monitor.decode_participants(participants())
        self.assertEqual(people[21]['name'], 'Cullen Louis')
        self.assertEqual(people[3]['team_id'], 12)
        result = monitor.decode_status(status(), 21)
        self.assertEqual((monitor.VISUAL_TYRES[result['visual']],
                          monitor.ACTUAL_TYRES[result['actual']], result['age']),
                         ('Hard', 'C1', 3))
        self.assertEqual(monitor.decode_tyre_sets(tyre_sets(), 21)['wear'], 15)
        self.assertIsNone(monitor.decode_tyre_sets(tyre_sets(), 3))

    def test_history_maps_lap_to_correct_stint_and_respects_validity(self):
        car, laps = monitor.decode_history(history(21,
            [(82., True), (81., True), (90., False), (80., True)],
            [(3, 20, 18), (255, 18, 16)]))
        self.assertEqual(car, 21)
        self.assertEqual([x['visual'] for x in laps], [18, 18, 18, 16])
        self.assertEqual([x['stint'] for x in laps], [1, 1, 1, 2])
        self.assertEqual([x['valid'] for x in laps], [True, True, False, True])
        with self.assertRaises(old.InvalidPacket):
            monitor.decode_history(history(21, [(82., True)], [(255, 20, 18)])[:-1])

    def test_integrates_with_v2_without_overwriting_prior_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            existing = path / 'old_recording.csv'
            existing.write_text('keep this unchanged\n')
            r = monitor.Monitor(path, graph=False)
            try:
                r.process(participants())
                r.process(lap())
                r.process(status())
                r.process(tyre_sets())
                r.process(history(21, [(81.676, True), (80.2, True)],
                                  [(1, 20, 18), (255, 18, 16)]))
                r.process(history(3, [(79.6, True)], [(255, 18, 16)]))
                r.process(lap(t=11., frame=2))
                r.process(status(t=11.5, visual=16, actual=18, age=1))
                r.process(tyre_sets(t=11.51, wear=5))
                r.process(telemetry(t=11., frame=2))
                r.process(lap(t=12., frame=3))
                r.process(telemetry(t=12., frame=3))
            finally:
                r.close()
            self.assertEqual(existing.read_text(), 'keep this unchanged\n')
            board = rows(next(path.glob('*_leaderboard.csv')))
            player = [row for row in board if row['car_index'] == '21'][-1]
            self.assertEqual(player['driver_name'], 'Cullen Louis')
            self.assertEqual(player['game_position'], '2')
            self.assertEqual(player['best_lap_rank'], '2')
            self.assertEqual(player['visual_tyre'], 'Soft')
            rival = [row for row in board if row['car_index'] == '3'][-1]
            self.assertEqual(rival['visual_tyre'], 'Soft')
            self.assertEqual(player['is_player'], '1')
            laps = rows(next(path.glob('*_laps.csv')))
            self.assertEqual(len(laps), 3)
            self.assertEqual([(row['lap_number'], row['visual_tyre']) for row in laps
                              if row['car_index'] == '21'], [('1', 'Hard'), ('2', 'Soft')])
            tyre = rows(next(path.glob('*_tyres.csv')))
            self.assertEqual(tyre[0]['visual_tyre'], 'Hard')
            self.assertEqual(tyre[-1]['visual_tyre'], 'Soft')
            raw = rows(next(path.glob('*_all.csv')))
            self.assertEqual(len(raw), 2)
            self.assertEqual(raw[0]['speed_kmh'], '240')
            self.assertEqual(len(rows(next(path.glob('*_ontrack_stint01.csv')))), 2)
            report = tyre_compare.compare(next(path.glob('*_laps.csv')))
            self.assertTrue(any('Hard' in text and '1 lap(s)' in text for text in report))
            self.assertTrue(any('Soft' in text and '1 lap(s)' in text for text in report))
            self.assertTrue(any('Observed best-lap differences' in text for text in report))

    def test_malformed_packet_lengths_rejected(self):
        for decode, args in ((monitor.decode_participants, (participants()[:-1],)),
                             (monitor.decode_status, (status()[:-1], 21)),
                             (monitor.decode_tyre_sets, (tyre_sets()[:-1], 21))):
            with self.assertRaises(old.InvalidPacket):
                decode(*args)

    def test_udp_socket_to_leaderboard_and_lap_files(self):
        with tempfile.TemporaryDirectory() as folder:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            outcomes = []
            worker = threading.Thread(target=lambda: outcomes.append(
                monitor.capture(port, 1.2, Path(folder), False)), daemon=True)
            worker.start()
            time.sleep(.15)
            packets = [participants(), status(),
                       lap(t=10, frame=1), telemetry(t=10, frame=1),
                       history(21, [(81.2, True)], [(255, 20, 18)], t=10.5),
                       lap(t=11, frame=2), telemetry(t=11, frame=2)]
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                for packet in packets:
                    sender.sendto(packet, ('127.0.0.1', port))
                    time.sleep(.02)
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes, [0])
            directory = Path(folder)
            self.assertGreater(len(rows(next(directory.glob('*_leaderboard.csv')))), 0)
            self.assertEqual(len(rows(next(directory.glob('*_laps.csv')))), 1)
            self.assertEqual(len(rows(next(directory.glob('*_all.csv')))), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
