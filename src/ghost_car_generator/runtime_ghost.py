"""Portable runtime ghost timeline for CSP Lua consumers.

The format intentionally contains no ``.acreplay`` bytes.  Python performs
coordinate conversion and smoothing; a Lua player only needs to interpolate
the resulting body pose and optional wheel-local state.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "ghost-runtime-v1"


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
    return {
        "schema": SCHEMA,
        "carId": str(car_id),
        "trackId": str(track_id),
        "frameIntervalS": 0.015,
        "frames": frames,
    }


def write_runtime_ghost(
    filename: str | Path, poses: Iterable[Mapping[str, Any]], *, car_id: str, track_id: str,
    wheel_state: Iterable[Mapping[str, Any]] | None = None,
) -> Path:
    """Write a deterministic UTF-8 runtime ghost file and return its path."""
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_runtime_ghost(
        poses, car_id=car_id, track_id=track_id, wheel_state=wheel_state
    )
    path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n", encoding="utf-8")
    return path
