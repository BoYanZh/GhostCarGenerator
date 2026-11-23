"""Unit tests for ghost-runtime-v1 builder and periodic smoothing."""
from __future__ import annotations

import math

import numpy as np

from ghost_car_generator.runtime_ghost import (
    SCHEMA,
    build_runtime_ghost,
    smooth_runtime_ghost,
)


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
