"""Unit tests for ghost-runtime-v1 builder, periodic smoothing, and CLI export."""
from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import numpy as np

from ghost_car_generator.acreplay import CAR_FRAME, VERSION
from ghost_car_generator.cli import main as cli_main
from ghost_car_generator.ld_replay import fit_pavement_offset
from ghost_car_generator.runtime_ghost import (
    DEFAULT_KN5_HEIGHT_OFFSET_M,
    SCHEMA,
    build_runtime_ghost,
    smooth_runtime_ghost,
)
from ghost_car_generator.track_surface import SURFACE_MAGIC, TrackSurface


def _circle_poses(count: int = 400, dt: float = 0.015, radius: float = 100.0, quantize_step: int = 0):
    poses = []
    for i in range(count):
        theta = 2.0 * math.pi * i / count
        if quantize_step > 1:
            # Simulate quantized GPS stair-stepping (position held for quantize_step frames).
            q = (i // quantize_step) * quantize_step
            theta_q = 2.0 * math.pi * q / count
            x = radius * math.cos(theta_q)
            z = radius * math.sin(theta_q)
        else:
            x = radius * math.cos(theta)
            z = radius * math.sin(theta)
        poses.append(
            {
                "timeS": i * dt,
                "positionM": [x, 1.5 * math.sin(theta), z],
                "rotationRad": [0.0, 0.0, 0.0],
                "steerAngleDeg": 12.0,
            }
        )
    return poses


def test_build_runtime_ghost_includes_lap_time_s():
    poses = _circle_poses(count=100, dt=0.015)
    payload = build_runtime_ghost(
        poses,
        car_id="ig_toyota_gr86_premium",
        track_id="thunderhill_raceway_park/threemilebypass",
        lap_time_s=1.500,
    )
    assert payload["schema"] == SCHEMA
    assert payload["lapTimeS"] == 1.500
    assert len(payload["frames"]) == 100


def test_smooth_runtime_ghost_removes_gps_staircase_and_preserves_lap_time():
    dt = 0.015
    count = 600
    raw = build_runtime_ghost(
        _circle_poses(count=count, dt=dt, quantize_step=5),
        car_id="ig_toyota_gr86_premium",
        track_id="thunderhill_raceway_park/threemilebypass",
        lap_time_s=count * dt,
    )

    def dv_p95(frames):
        pts = np.array([f["positionM"] for f in frames], dtype=np.float64)
        steps = np.linalg.norm(np.diff(pts, axis=0), axis=1) / dt
        return float(np.percentile(np.abs(np.diff(steps)), 95))

    raw_dv = dv_p95(raw["frames"])
    smoothed = smooth_runtime_ghost(raw, window_frames=(51, 35), poly_order=3, lap_time_s=count * dt)
    smooth_dv = dv_p95(smoothed["frames"])

    assert raw_dv > 50.0
    assert smooth_dv < 0.5
    assert smoothed["lapTimeS"] == count * dt
    assert len(smoothed["frames"]) == count
    assert smoothed["frames"][0]["timeS"] == 0.0
    assert smoothed["frames"][-1]["timeS"] == raw["frames"][-1]["timeS"]
    # Loop seam should remain close after circular smoothing.
    seam_m = math.dist(smoothed["frames"][-1]["positionM"], smoothed["frames"][0]["positionM"])
    assert seam_m < 3.0


def test_fit_pavement_offset_recovers_global_gps_shift():
    # Build a synthetic rectangular asphalt loop centered on a 50 m circle (width 10 m).
    angles = np.linspace(0.0, 2.0 * math.pi, 120, endpoint=False)
    ref = np.column_stack([50.0 * np.cos(angles), np.zeros_like(angles), 50.0 * np.sin(angles)])
    verts = []
    tris = []
    for i in range(len(angles)):
        a = angles[i]
        inner = [45.0 * math.cos(a), 0.0, 45.0 * math.sin(a)]
        outer = [55.0 * math.cos(a), 0.0, 55.0 * math.sin(a)]
        verts.append(inner)
        verts.append(outer)
    n = len(angles)
    for i in range(n):
        j = (i + 1) % n
        tris.append([2 * i, 2 * i + 1, 2 * j + 1])
        tris.append([2 * i, 2 * j + 1, 2 * j])
    surface = TrackSurface(np.array(verts, dtype=np.float64), np.array(tris, dtype=np.int64))

    # Shift the driven path off-road by (+6.0, -4.0) m (>5 m half-width) to mimic GPS receiver bias.
    biased = ref.copy()
    biased[:, 0] += 6.0
    biased[:, 2] -= 4.0
    corrected, diag = fit_pavement_offset(
        biased, surface, ref, search_radius_m=8.0, coarse_step_m=1.0, fine_step_m=0.5
    )

    assert diag["pavementOnRoadBefore"] < 0.75
    assert diag["pavementOnRoadAfter"] == 1.0
    assert diag["pavementBothWheelsAfter"] == 1.0
    assert abs(diag["pavementOffsetAcM"]["x"] - (-6.0)) <= 1.0
    assert abs(diag["pavementOffsetAcM"]["z"] - 4.0) <= 1.0
    assert diag["pavementRefRmsAfterM"] < 1.0
    assert corrected.shape == biased.shape


def _write_surface_file(path: Path, vertices: np.ndarray, triangles: np.ndarray) -> None:
    verts = np.asarray(vertices, dtype="<f4")
    tris = np.asarray(triangles, dtype="<u4")
    with path.open("wb") as handle:
        handle.write(SURFACE_MAGIC)
        handle.write(struct.pack("<II", len(verts), len(tris)))
        handle.write(verts.tobytes())
        handle.write(tris.tobytes())


def _build_minimal_acreplay(num_frames: int = 12) -> bytes:
    def lstring(text: str) -> bytes:
        raw = text.encode("utf-8")
        return struct.pack("<I", len(raw)) + raw

    out = bytearray()
    out += struct.pack("<Id", VERSION, 15.0)
    out += lstring("sol_03_scattered_clouds")
    out += lstring("test_track")
    out += lstring("loop")
    out += struct.pack("<IIII", 1, num_frames, num_frames, 0)
    out += b"\x00" * (4 * num_frames)
    out += lstring("ig_toyota_gr86_premium")
    out += lstring("Test Driver")
    out += lstring("USA")
    out += lstring("Team")
    out += lstring("default_skin")
    out += struct.pack("<II", num_frames, 0)
    for i in range(num_frames):
        out += struct.pack("<I4f", i * 15, 22.0, 28.0, 0.0, 0.0)
        vals = [0.0] * 106
        vals[0:3] = (float(i), 1.0, float(i) * 2.0)
        vals[3:6] = (0.0, 0.25, 0.0)
        vals[54:57] = (10.0, 0.0, 20.0)
        vals[78] = 5.0
        for idx in range(81, len(vals)):
            vals[idx] = int(vals[idx])
        vals[105] = 1
        out += CAR_FRAME.pack(*vals)
    out += struct.pack("<I", 0)
    return bytes(out)


def test_export_runtime_ghost_from_acreplay_cli(tmp_path: Path):
    replay_path = tmp_path / "sample.acreplay"
    out_path = tmp_path / "ghost-runtime-v1.json"
    replay_path.write_bytes(_build_minimal_acreplay(num_frames=12))

    cli_main(
        [
            "replay",
            "export-runtime",
            str(replay_path),
            "-o",
            str(out_path),
            "--height-offset-m",
            "-0.25",
            "--lap-time-s",
            "0.180",
        ]
    )

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["schema"] == SCHEMA
    assert payload["carId"] == "ig_toyota_gr86_premium"
    assert payload["trackId"] == "test_track/loop"
    assert payload["lapTimeS"] == 0.18
    assert len(payload["frames"]) == 12
    assert math.isclose(payload["frames"][0]["positionM"][1], 0.75, abs_tol=1e-4)


def test_export_runtime_ghost_from_ld_cli(tmp_path: Path, monkeypatch):
    # Create a synthetic track package with a 50 m circular asphalt loop at y = 2.0 m.
    angles = np.linspace(0.0, 2.0 * math.pi, 120, endpoint=False)
    ref = np.column_stack([50.0 * np.cos(angles), np.full_like(angles, 2.0), 50.0 * np.sin(angles)])
    verts = []
    tris = []
    for a in angles:
        verts.append([45.0 * math.cos(a), 2.0, 45.0 * math.sin(a)])
        verts.append([55.0 * math.cos(a), 2.0, 55.0 * math.sin(a)])
    n = len(angles)
    for i in range(n):
        j = (i + 1) % n
        tris.append([2 * i, 2 * i + 1, 2 * j + 1])
        tris.append([2 * i, 2 * j + 1, 2 * j])

    pkg_dir = tmp_path / "track_pkg"
    pkg_dir.mkdir()
    _write_surface_file(pkg_dir / "road-pit.gcsurface", np.array(verts), np.array(tris))
    (pkg_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "track": {"name": "synth_track", "layout": "loop"},
                "trackReference": "calibration.json",
                "surfaces": ["road-pit.gcsurface"],
            }
        ),
        encoding="utf-8",
    )
    # Identity ENU->AC mapping (E -> X, N -> Z, U -> Y) with startLineAc at angle=pi/2 -> (0, 2, 50).
    (pkg_dir / "calibration.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "track": {"name": "synth_track", "layout": "loop"},
                "origin": {"latitudeDeg": 39.0, "longitudeDeg": -122.0, "altitudeM": 0.0},
                "enuToAc": {
                    "matrix": [
                        [1.0, 0.0, 0.0, 0.0],
                        [0.0, 0.0, 1.0, 0.0],
                        [0.0, 1.0, 0.0, 0.0],
                        [0.0, 0.0, 0.0, 1.0],
                    ]
                },
                "startLineAc": [0.0, 2.0, 50.0],
                "referencePathAc": ref.tolist(),
            }
        ),
        encoding="utf-8",
    )

    # Synthetic 10s MoTeC lap at 25 Hz starting at angle=0, shifted by (+5.0, -3.0) m.
    sample_count = 250
    lap_time_s = 10.0
    t_angles = np.linspace(0.0, 2.0 * math.pi, sample_count, endpoint=False)
    fake_points = {
        "selectedLap": 3,
        "lapTimeS": lap_time_s,
        "frequencyHz": 25.0,
        "origin": {"latitudeDeg": 39.0, "longitudeDeg": -122.0, "altitudeM": 0.0},
        "points": [
            {
                "timeS": float(i) / 25.0,
                "xM": float(50.0 * math.cos(a) + 5.0),
                "yM": float(50.0 * math.sin(a) - 3.0),
                "zM": 0.0,
                "speedMS": 31.4,
                "steeringDeg": 15.0,
            }
            for i, a in enumerate(t_angles)
        ],
    }

    import ghost_car_generator.motec as motec_mod

    monkeypatch.setattr(motec_mod, "extract_motec_points", lambda *a, **kw: fake_points)

    dummy_ld = tmp_path / "demo.ld"
    dummy_ld.write_bytes(b"")
    out_path = tmp_path / "out_ld.json"

    cli_main(
        [
            "replay",
            "export-runtime",
            str(dummy_ld),
            "--gps-track",
            str(pkg_dir),
            "--car-id",
            "ig_toyota_gr86_premium",
            "-o",
            str(out_path),
        ]
    )

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["schema"] == SCHEMA
    assert payload["carId"] == "ig_toyota_gr86_premium"
    assert payload["trackId"] == "synth_track/loop"
    assert payload["lapTimeS"] == lap_time_s
    assert len(payload["frames"]) > 600
    # Frame 0 should be circularly aligned near startLineAc = (0, 2 - 0.4645, 50).
    p0 = payload["frames"][0]["positionM"]
    assert math.hypot(p0[0] - 0.0, p0[2] - 50.0) < 2.0
    assert math.isclose(p0[1], 2.0 + DEFAULT_KN5_HEIGHT_OFFSET_M, abs_tol=0.05)
