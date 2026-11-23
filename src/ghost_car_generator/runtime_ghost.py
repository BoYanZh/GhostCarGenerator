"""Portable runtime ghost timeline for CSP Lua consumers.

The format intentionally contains no ``.acreplay`` bytes.  Python performs
coordinate conversion and smoothing; a Lua player only needs to interpolate
the resulting body pose and optional wheel-local state.
"""
from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

SCHEMA = "ghost-runtime-v1"


def _periodic_savgol(values: np.ndarray, window: int, poly_order: int = 3) -> np.ndarray:
    """Zero-phase periodic Savitzky-Golay smoothing over a closed lap."""
    arr = np.asarray(values, dtype=np.float64)
    n = len(arr)
    if n < 5 or window < 5:
        return arr.copy()
    w = min(int(window), n if n % 2 == 1 else n - 1)
    if w % 2 == 0:
        w -= 1
    if w <= poly_order:
        return arr.copy()
    half = w // 2
    coords = np.arange(-half, half + 1, dtype=np.float64)
    design = np.vander(coords, poly_order + 1, increasing=True)
    weights = np.linalg.pinv(design)[0]
    padded = np.concatenate([arr[-half:], arr, arr[:half]])
    return np.convolve(padded, weights[::-1], mode="valid")


def smooth_runtime_ghost(
    payload: Mapping[str, Any],
    *,
    window_frames: Sequence[int] = (51, 35),
    poly_order: int = 3,
    lap_time_s: float | None = None,
) -> dict[str, Any]:
    """Return a zero-phase smoothed copy of a ``ghost-runtime-v1`` payload.

    Removes high-frequency GPS quantization stair-stepping while keeping exact
    ``timeS`` timestamps (zero phase / lap-time shift) and C2-continuous loop
    closure across the start/finish wrap.
    """
    result = deepcopy(dict(payload))
    frames = result.get("frames") or []
    count = len(frames)
    if count < 5:
        if lap_time_s is not None:
            result["lapTimeS"] = float(lap_time_s)
        return result

    dt = float(result.get("frameIntervalS") or 0.015)
    pos = np.array([fr["positionM"] for fr in frames], dtype=np.float64)
    smoothed = pos.copy()
    for win in window_frames:
        for axis in range(3):
            smoothed[:, axis] = _periodic_savgol(smoothed[:, axis], int(win), poly_order)

    vel = (np.roll(smoothed, -1, axis=0) - np.roll(smoothed, 1, axis=0)) / (2.0 * dt)
    horiz = np.hypot(vel[:, 0], vel[:, 2])
    yaw = np.arctan2(-vel[:, 0], vel[:, 2])
    pitch = np.arctan2(vel[:, 1], np.maximum(horiz, 1e-6))

    for i, fr in enumerate(frames):
        roll = float(fr["rotationRad"][2]) if len(fr.get("rotationRad", [])) >= 3 else 0.0
        fr["positionM"] = [round(float(smoothed[i, 0]), 4), round(float(smoothed[i, 1]), 4), round(float(smoothed[i, 2]), 4)]
        fr["rotationRad"] = [round(float(pitch[i]), 6), round(float(yaw[i]), 6), round(roll, 6)]
        fr["velocityMS"] = [round(float(vel[i, 0]), 4), round(float(vel[i, 1]), 4), round(float(vel[i, 2]), 4)]

    if lap_time_s is not None:
        result["lapTimeS"] = float(lap_time_s)
    elif "lapTimeS" not in result:
        result["lapTimeS"] = round(float(frames[-1]["timeS"]) + dt, 6)
    return result


def _finite_vector(value: Any, size: int, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError(f"{name} must contain {size} numbers")
    result = [float(item) for item in value]
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{name} must be finite")
    return result


def build_runtime_ghost(
    poses: Iterable[Mapping[str, Any]],
    *,
    car_id: str,
    track_id: str,
    wheel_state: Iterable[Mapping[str, Any]] | None = None,
    lap_time_s: float | None = None,
) -> dict[str, Any]:
    """Convert pose dictionaries into the stable Lua-facing JSON schema."""
    pose_list = list(poses)
    wheel_list = list(wheel_state) if wheel_state is not None else None
    if wheel_list is not None and len(wheel_list) != len(pose_list):
        raise ValueError("wheel_state must have one item per pose")
    frames: list[dict[str, Any]] = []
    for index, pose in enumerate(pose_list):
        position = _finite_vector(pose.get("positionM"), 3, "positionM")
        rotation = _finite_vector(pose.get("rotationRad"), 3, "rotationRad")
        frame: dict[str, Any] = {
            "timeS": float(pose.get("timeS", index * 0.015)),
            "positionM": position,
            "rotationRad": rotation,
            "steerAngleDeg": float(pose.get("steerAngleDeg", 0.0)),
            "velocityMS": _finite_vector(
                pose.get("velocityMS", [0.0, 0.0, 0.0]), 3, "velocityMS"
            ),
        }
        if not math.isfinite(frame["timeS"]) or frame["timeS"] < 0:
            raise ValueError("timeS must be finite and non-negative")
        if wheel_list is not None:
            wheel = wheel_list[index]
            frame["wheelLocalPositionM"] = [
                _finite_vector(item, 3, "wheelLocalPositionM")
                for item in wheel.get("wheelLocalPositionM", [])
            ]
            frame["wheelLocalRotationRad"] = [
                _finite_vector(item, 3, "wheelLocalRotationRad")
                for item in wheel.get("wheelLocalRotationRad", [])
            ]
            frame["wheelRollRad"] = [
                float(item) for item in wheel.get("wheelRollRad", [0.0] * 4)
            ]
        frames.append(frame)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "carId": str(car_id),
        "trackId": str(track_id),
        "frameIntervalS": 0.015,
        "frames": frames,
    }
    if lap_time_s is not None:
        if not math.isfinite(float(lap_time_s)) or float(lap_time_s) <= 0:
            raise ValueError("lap_time_s must be finite and positive")
        payload["lapTimeS"] = float(lap_time_s)
    return payload


def write_runtime_ghost(
    filename: str | Path, poses: Iterable[Mapping[str, Any]], *, car_id: str, track_id: str,
    wheel_state: Iterable[Mapping[str, Any]] | None = None,
    lap_time_s: float | None = None,
) -> Path:
    """Write a deterministic UTF-8 runtime ghost file and return its path."""
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_runtime_ghost(
        poses, car_id=car_id, track_id=track_id, wheel_state=wheel_state,
        lap_time_s=lap_time_s,
    )
    path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n", encoding="utf-8")
    return path
