"""Add-on F1 25 (2026 UDP) leaderboard and tyre monitor.

Put beside f1_25_udp_v2.py and f1_25_udp.py. Run this INSTEAD of v2,
not simultaneously: python3 f1_25_monitor.py --seconds 600
Keeps v2's raw/on-track/pit recordings, adds leaderboard/laps/tyres CSVs.
"""
from __future__ import annotations

import argparse
import csv
import math
import socket
import struct
import sys
import time
from pathlib import Path

import f1_25_udp_v2 as v2
from f1_25_udp import HEADER, InvalidPacket

PARTICIPANTS_SIZE, PARTICIPANT_STRIDE = 1470, 60
CAR_STATUS_SIZE, CAR_STATUS_STRIDE = 1445, 59
HISTORY_SIZE, HISTORY_LAPS, HISTORY_LAP_STRIDE = 1460, 100, 14
TYRE_SETS_SIZE, TYRE_SET_STRIDE = 231, 10

VISUAL_TYRES = {16: 'Soft', 17: 'Medium', 18: 'Hard', 7: 'Intermediate', 8: 'Wet'}
ACTUAL_TYRES = {16: 'C5', 17: 'C4', 18: 'C3', 19: 'C2', 20: 'C1', 21: 'C0', 22: 'C6', 7: 'Intermediate', 8: 'Wet'}

LEADERBOARD_FIELDS = (
    'session_uid', 'timeline', 'session_time_s', 'session_type_code', 'car_index', 'is_player',
    'driver_name', 'team_id', 'game_position', 'best_lap_rank',
    'lap_number', 'best_valid_lap_s', 'delta_to_fastest_lap_s',
    'last_lap_s', 'lap_distance_m', 'pit_status', 'gap_to_car_ahead_s',
    'gap_to_leader_s', 'visual_tyre', 'actual_tyre', 'tyre_age_laps',
)
LAP_FIELDS = (
    'session_uid', 'timeline', 'car_index', 'is_player', 'driver_name', 'team_id', 'lap_number',
    'lap_time_s', 'valid_lap', 'visual_tyre', 'actual_tyre', 'tyre_stint_number',
)
TYRE_FIELDS = (
    'session_uid', 'timeline', 'session_time_s', 'car_index', 'lap_number',
    'visual_tyre', 'actual_tyre', 'tyre_age_laps', 'fitted_set_wear_pct',
    'fitted_set_life_laps',
)


def _check(packet: bytes, required: int, what: str) -> None:
    if len(packet) != required:
        raise InvalidPacket(f'{what}: expected {required} bytes, got {len(packet)}')


def decode_participants(packet: bytes) -> dict[int, dict]:
    _check(packet, PARTICIPANTS_SIZE, 'Participants')
    count = packet[HEADER.size]
    if count > 24:
        raise InvalidPacket(f'Invalid active participant count: {count}')
    out = {}
    for i in range(count):
        start = HEADER.size + 1 + i * PARTICIPANT_STRIDE
        name_bytes = packet[start + 10:start + 42].split(b'\0', 1)[0]
        name = name_bytes.decode('utf-8', errors='replace').strip()
        out[i] = {'name': name, 'team_id': struct.unpack_from('<H', packet, start + 5)[0]}
    return out


def decode_status(packet: bytes, player: int) -> dict:
    _check(packet, CAR_STATUS_SIZE, 'Car Status')
    start = HEADER.size + player * CAR_STATUS_STRIDE
    actual, visual, age = packet[start + 25:start + 28]
    return {'actual': actual, 'visual': visual, 'age': age}


def decode_tyre_sets(packet: bytes, player: int) -> dict | None:
    _check(packet, TYRE_SETS_SIZE, 'Tyre Sets')
    if packet[HEADER.size] != player:
        return None
    idx = packet[-1]
    if idx >= 20:
        return None
    start = HEADER.size + 1 + idx * TYRE_SET_STRIDE
    fitted = packet[start + 9]
    if fitted not in (0, 1) or not fitted:
        return None
    wear = packet[start + 2]
    return {'wear': wear if wear <= 100 else None, 'life': packet[start + 5]}


def decode_history(packet: bytes) -> tuple[int, list[dict]]:
    _check(packet, HISTORY_SIZE, 'Session History')
    start = HEADER.size
    car, n_laps, n_stints = packet[start:start + 3]
    if car >= 24 or n_laps > HISTORY_LAPS or n_stints > 8:
        raise InvalidPacket('Invalid Session History counts/index')
    tyre_base = start + 7 + HISTORY_LAPS * HISTORY_LAP_STRIDE
    stints = [tuple(packet[tyre_base + 3 * i:tyre_base + 3 * i + 3])
              for i in range(n_stints)]
    laps = []
    for lap_index in range(n_laps):
        pos = start + 7 + lap_index * HISTORY_LAP_STRIDE
        ms = struct.unpack_from('<I', packet, pos)[0]
        if ms == 0:  # Current incomplete lap has no completed time.
            continue
        validity = packet[pos + 13]
        stint_index = None
        compound = None
        for j, (end_lap, actual, visual) in enumerate(stints, start=1):
            if end_lap == 255 or lap_index + 1 <= end_lap:
                stint_index = j
                compound = (actual, visual)
                break
        laps.append({'lap': lap_index + 1, 'seconds': ms / 1000,
                     'valid': bool(validity & 1), 'stint': stint_index,
                     'actual': compound[0] if compound else None,
                     'visual': compound[1] if compound else None})
    return car, laps


class Monitor(v2.Recorder):
    def __init__(self, out_dir: Path, graph: bool = True):
        super().__init__(out_dir, graph)
        out_dir.mkdir(parents=True, exist_ok=True)
        prefix = f'f1_2026_{self.run_id}'
        self._extra = []
        for kind, fields in (('leaderboard', LEADERBOARD_FIELDS),
                             ('laps', LAP_FIELDS), ('tyres', TYRE_FIELDS)):
            path = out_dir / f'{prefix}_{kind}.csv'
            fh = path.open('x', newline='', encoding='utf-8')
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            setattr(self, f'{kind}_writer', writer)
            self._extra.append(fh)
            print(f'Saving {kind}: {path}')
        self.observed_uid = None
        self.names = {}
        self.status = {}
        self.status_t = None
        self.set_data = None
        self.set_t = None
        self.best = {}
        self.seen_laps = set()
        self.last_board_second = None
        self.last_console_bucket = None
        self.last_tyre_second = None
        self.last_observed_t = None
        self.last_observed_overall = None
        self.timeline = 0
        self.leaderboard_rows = self.lap_rows = self.tyre_rows = 0

    def _reset_observer(self, uid: int) -> None:
        self.observed_uid = uid
        self.names = {}
        self.status = {}
        self.status_t = None
        self.set_data = None
        self.set_t = None
        self.best = {}
        self.seen_laps = set()
        self.last_board_second = None
        self.last_console_bucket = None
        self.last_tyre_second = None
        self.last_observed_t = None
        self.last_observed_overall = None
        self.timeline += 1

    def _name(self, index: int) -> dict:
        return self.names.get(index, {})

    def process(self, packet: bytes) -> bool:
        h = v2.header(packet)
        if self.observed_uid != h['uid']:
            self._reset_observer(h['uid'])
        # Only a genuinely newer frame with rewound game time starts another timeline;
        # an old/reordered datagram must not erase the history we have accumulated.
        elif (self.last_observed_t is not None and self.last_observed_overall is not None
              and h['overall'] > self.last_observed_overall
              and h['time'] < self.last_observed_t - 1.0):
            self._reset_observer(h['uid'])
        if self.last_observed_t is None or h['time'] > self.last_observed_t:
            self.last_observed_t = h['time']
        if self.last_observed_overall is None or h['overall'] > self.last_observed_overall:
            self.last_observed_overall = h['overall']
        kind = h['id']
        if kind == 4:
            self.names = decode_participants(packet)
            return False
        if kind == 7:
            # Car Status reports 24 cars, so display each rival's own fitted tyres.
            self.status = {i: decode_status(packet, i) for i in range(24)}
            self.status_t = h['time']
            status = self.status[h['player']]
            second = int(h['time'])
            if second != self.last_tyre_second:
                self.last_tyre_second = second
                lap = self.last_lap['player'] if self.last_lap and self.last_lap['header']['uid'] == h['uid'] and abs(h['time'] - self.last_lap['header']['time']) < 0.2 else None
                fitted = self.set_data if self.set_data and self.set_t is not None and 0 <= h['time'] - self.set_t < 3 else None
                self.tyres_writer.writerow({
                    'session_uid': h['uid'], 'timeline': self.timeline,
                    'session_time_s': round(h['time'], 3),
                    'car_index': h['player'], 'lap_number': lap['lap_number'] if lap else '',
                    'visual_tyre': VISUAL_TYRES.get(status['visual'], ''),
                    'actual_tyre': ACTUAL_TYRES.get(status['actual'], ''),
                    'tyre_age_laps': status['age'] if status['visual'] in VISUAL_TYRES else '',
                    'fitted_set_wear_pct': fitted['wear'] if fitted and fitted['wear'] is not None else '',
                    'fitted_set_life_laps': fitted['life'] if fitted else '',
                })
                self._extra[2].flush()
                self.tyre_rows += 1
            return False
        if kind == 12:
            self.set_data = decode_tyre_sets(packet, h['player'])
            self.set_t = h['time'] if self.set_data is not None else None
            return False
        if kind == 11:
            car, laps = decode_history(packet)
            who = self._name(car)
            for lap in laps:
                key = (self.timeline, car, lap['lap'])
                if key in self.seen_laps:
                    continue
                # A completed lap is emitted once, using the history's matching tyre stint.
                self.seen_laps.add(key)
                self.laps_writer.writerow({
                    'session_uid': h['uid'], 'timeline': self.timeline, 'car_index': car,
                    'is_player': int(car == h['player']),
                    'driver_name': who.get('name', ''), 'team_id': who.get('team_id', ''),
                    'lap_number': lap['lap'], 'lap_time_s': lap['seconds'],
                    'valid_lap': int(lap['valid']),
                    'visual_tyre': VISUAL_TYRES.get(lap['visual'], '') if lap['visual'] is not None else '',
                    'actual_tyre': ACTUAL_TYRES.get(lap['actual'], '') if lap['actual'] is not None else '',
                    'tyre_stint_number': lap['stint'] if lap['stint'] is not None else '',
                })
                self.lap_rows += 1
                if lap['valid'] and lap['seconds'] > 0:
                    self.best[car] = min(self.best.get(car, float('inf')), lap['seconds'])
            self._extra[1].flush()
            return False
        if kind == 2:
            # v2 also processes this packet for the existing player-car CSVs.
            saved = super().process(packet)
            second = int(h['time'])
            if second == self.last_board_second:
                return saved
            self.last_board_second = second
            cars = [v2.lap_car(packet, i) for i in range(24)]
            active = [i for i, car in enumerate(cars)
                      if car['result_status'] in (2, 3) and 1 <= (car['position'] or 0) <= 24]
            ordered_best = sorted((self.best[i], i) for i in active if i in self.best)
            ranks = {idx: rank for rank, (_, idx) in enumerate(ordered_best, start=1)}
            fastest = ordered_best[0][0] if ordered_best else None
            session_type = self.last_session['session_type'] if self.last_session and self.last_session['header']['uid'] == h['uid'] else ''
            for i in sorted(active, key=lambda index: cars[index]['position']):
                car = cars[i]
                who = self._name(i)
                tyre = self.status.get(i) if self.status_t is not None and abs(h['time'] - self.status_t) < 1 else None
                best = self.best.get(i)
                self.leaderboard_writer.writerow({
                    'session_uid': h['uid'], 'timeline': self.timeline,
                    'session_time_s': round(h['time'], 3),
                    'session_type_code': session_type,
                    'car_index': i, 'is_player': int(i == h['player']),
                    'driver_name': who.get('name', ''),
                    'team_id': who.get('team_id', ''),
                    'game_position': car['position'],
                    'best_lap_rank': ranks.get(i, ''), 'lap_number': car['lap_number'] or '',
                    'best_valid_lap_s': best if best is not None else '',
                    'delta_to_fastest_lap_s': round(best - fastest, 3) if best is not None and fastest is not None else '',
                    'last_lap_s': car['last_lap_time_s'] if car['last_lap_time_s'] else '',
                    'lap_distance_m': car['lap_distance_m'],
                    'pit_status': car['pit_status'],
                    'gap_to_car_ahead_s': car['gap_ahead_s'] if car['gap_ahead_s'] is not None else '',
                    'gap_to_leader_s': car['gap_to_leader_s'] if car['gap_to_leader_s'] is not None else '',
                    'visual_tyre': VISUAL_TYRES.get(tyre['visual'], '') if tyre else '',
                    'actual_tyre': ACTUAL_TYRES.get(tyre['actual'], '') if tyre else '',
                    'tyre_age_laps': tyre['age'] if tyre and tyre['visual'] in VISUAL_TYRES else '',
                })
                self.leaderboard_rows += 1
            self._extra[0].flush()
            # Small, readable console snapshot every ten game seconds.
            bucket = second // 10
            if bucket != self.last_console_bucket:
                self.last_console_bucket = bucket
                print('LEADERBOARD | game positions (best-lap ranks are separate)')
                for i in sorted(active, key=lambda index: cars[index]['position'])[:5]:
                    car = cars[i]
                    name = self._name(i).get('name') or f'car index {i}'
                    best = self.best.get(i)
                    best_text = f'{int(best // 60)}:{best % 60:06.3f}' if best is not None else '--'
                    tyre = (self.status.get(i) if self.status_t is not None
                            and abs(h['time'] - self.status_t) < 1 else None)
                    tyre_text = VISUAL_TYRES.get(tyre['visual'], '?') if tyre else '?'
                    print(f"  P{car['position']}: {name} | best captured {best_text} | tyres {tyre_text}")
            return saved
        return super().process(packet)

    def close(self) -> None:
        try:
            super().close()  # V2 saves untrimmed & on-track data and generates its graphs.
        finally:
            for fh in self._extra:
                fh.close()
            print(f'Monitor: {self.leaderboard_rows} leaderboard rows, '
                  f'{self.lap_rows} completed-lap rows, {self.tyre_rows} tyre snapshots')


def capture(port: int, seconds: float, out_dir: Path, graph: bool) -> int:
    monitor = Monitor(out_dir, graph=graph)
    datagrams = invalid = recorded = 0
    end = time.monotonic() + seconds if seconds else None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(('0.0.0.0', port))
            sock.settimeout(0.5)
            print(f'Listening on UDP {port} (2026 format). Only run ONE listener.')
            while end is None or time.monotonic() < end:
                try:
                    packet, address = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                datagrams += 1
                if datagrams == 1:
                    print(f'First UDP datagram from {address[0]}:{address[1]}')
                try:
                    if monitor.process(packet):
                        recorded += 1
                        if recorded % 600 == 0:
                            print(f'LIVE | player samples {recorded} | leaderboard rows {monitor.leaderboard_rows} '
                                  f'| tyre rows {monitor.tyre_rows} | completed lap rows {monitor.lap_rows}')
                except InvalidPacket as exc:
                    invalid += 1
                    if invalid <= 6:
                        print(f'Ignored invalid packet: {exc}', file=sys.stderr)
    except KeyboardInterrupt:
        print('Stopped by user.')
    except OSError as exc:
        print(f'Network or file error: {exc}', file=sys.stderr)
        return 1
    finally:
        monitor.close()
        print(f'Datagrams {datagrams}; player samples {recorded}; rejected packets {invalid}')
    return 0 if recorded else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=20777)
    parser.add_argument('--seconds', type=float, default=0,
                        help='Real-time capture duration, 0 = until Ctrl+C')
    parser.add_argument('--output-dir', type=Path, default=Path('recordings'))
    parser.add_argument('--no-graph', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error('invalid port or duration')
    return capture(args.port, args.seconds, args.output_dir, not args.no_graph)


if __name__ == '__main__':
    raise SystemExit(main())
