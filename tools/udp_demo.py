"""
Send synthetic F1 25 UDP telemetry to Box-Box — try the driving coach without the game.

Plays laps on a made-up 4.2 km track with driving mistakes baked in (braking too deep,
lock-ups, wheelspin, understeer, ...) as real F1 25 packets (format 2025, 20 Hz).

  python tools/udp_demo.py               # 6 laps at real-time speed to 127.0.0.1:20777
  python tools/udp_demo.py --speed 8     # 8x faster
"""
from __future__ import annotations

import argparse
import ctypes
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import f1_parser as P  # noqa: E402
import sim_track as T  # noqa: E402

LAY = P.layouts(2025)
N = LAY["num_cars"]
PLAYER = 0
SESSION_UID = 0xB0B0B0
DEMO_LAPS = [
  {}, {},
  {"deep": 3, "lockup": 1},
  {"spin": 5, "under": 4},
  {"early": 2, "hot": 4},
  {"deep": 3, "spin": 5},
]
WHEEL_ORDER = ("RL", "RR", "FL", "FR")


def header(packet_id: int, t: float, frame: int) -> bytes:
  h = P.PacketHeader()
  h.packet_format, h.game_year, h.game_major_version, h.packet_version = 2025, 25, 1, 1
  h.packet_id, h.session_uid, h.session_time = packet_id, SESSION_UID, t
  h.frame_identifier = h.overall_frame_identifier = frame
  h.player_car_index, h.secondary_player_car_index = PLAYER, 255
  return bytes(h)


def cars(cls, fill) -> bytes:
  arr = (cls * N)()
  fill(arr)
  return bytes(arr)


def session_packet(t, frame) -> bytes:
  s = P.SessionPrefix()
  s.weather, s.track_temperature, s.air_temperature = 0, 34, 24
  s.total_laps, s.track_length, s.session_type, s.track_id = len(DEMO_LAPS), int(T.TRACK_LEN), 1, 26
  return header(1, t, frame) + bytes(s) + bytes(64)


def participants_packet(t, frame) -> bytes:
  def fill(a):
    for i in range(N):
      a[i].race_number = 1 + i
      a[i].name = (b"YOU" if i == PLAYER else f"AI {i}".encode())
  return header(4, t, frame) + bytes([1]) + cars(LAY["participant"], fill)


def packets_for(s: dict, frame: int) -> list[bytes]:
  t = s["t"]

  def lap_fill(a):
    d = a[PLAYER]
    d.current_lap_num, d.lap_distance, d.total_distance = s["lap"], s["dist"], s["dist"]
    d.current_lap_time_in_ms, d.last_lap_time_in_ms = s["lap_ms"], s["last_lap_ms"]
    d.car_position, d.result_status = 1, 2

  def motion_fill(a):
    a[PLAYER].world_position_x, a[PLAYER].world_position_z = s["x"], s["z"]
    a[PLAYER].world_forward_dir_z = 32767

  def tele_fill(a):
    c = a[PLAYER]
    c.speed, c.throttle, c.brake, c.steer, c.gear = s["speed"], s["throttle"], s["brake"], s["steer"], s["gear"]
    c.engine_rpm = 9000 + int(s["speed"]) * 10
    for j in range(4):
      c.surface_type[j] = s["surface"][j]
      c.tyres_surface_temperature[j] = s["tyre_surface"][j]
      c.tyres_inner_temperature[j] = 98

  def status_fill(a):
    c = a[PLAYER]
    c.fuel_in_tank, c.fuel_remaining_laps = 60.0, 2.0
    c.actual_tyre_compound, c.visual_tyre_compound, c.tyres_age_laps = 18, 17, s["lap"]
    c.ers_store_energy = 3_000_000

  def damage_fill(a):
    for j, w in enumerate(WHEEL_ORDER):
      a[PLAYER].tyres_wear[j] = s["wear"][w]

  ex = P.MotionExPrefix()
  for j in range(4):
    ex.wheel_slip_ratio[j] = s["slip_ratio"][j]
    ex.wheel_slip_angle[j] = s["slip_angle"][j]

  out = [
    header(2, t, frame) + cars(LAY["lap"], lap_fill) + bytes(2),
    header(0, t, frame) + cars(LAY["motion"], motion_fill),
    header(13, t, frame) + bytes(ex) + bytes(80),
    header(6, t, frame) + cars(LAY["telemetry"], tele_fill) + bytes(3),
  ]
  if frame % 10 == 0:
    out.append(header(7, t, frame) + cars(LAY["status"], status_fill))
    out.append(header(10, t, frame) + cars(LAY["damage"], damage_fill))
  return out


def main():
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--host", default="127.0.0.1")
  ap.add_argument("--port", type=int, default=int(os.getenv("F1_PORT", "20777")))
  ap.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier")
  ap.add_argument("--hz", type=int, default=20)
  args = ap.parse_args()

  samples = T.session(DEMO_LAPS, hz=args.hz)
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  dest = (args.host, args.port)
  print(f"Sending {len(DEMO_LAPS)} synthetic F1 25 laps to {dest[0]}:{dest[1]} at {args.speed}x …")
  start, t0 = time.time(), samples[0]["t"]
  for frame, s in enumerate(samples):
    if frame % args.hz == 0:
      sock.sendto(session_packet(s["t"], frame), dest)
      sock.sendto(participants_packet(s["t"], frame), dest)
    for pkt in packets_for(s, frame):
      sock.sendto(pkt, dest)
    target = start + (s["t"] - t0) / args.speed
    delay = target - time.time()
    if delay > 0:
      time.sleep(delay)
    elif frame % 50 == 0:
      time.sleep(0.001)  # let the receiver breathe when running flat out
    if s["dist"] < 1 and s["lap"] > 1:
      print(f"  lap {s['lap'] - 1} done")
  print("Done.")


if __name__ == "__main__":
  main()
