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
DEFAULT_FRAME_INTERVAL_S = 0.015
DEFAULT_KN5_HEIGHT_OFFSET_M = -0.4645


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
    frame_interval_s: float = DEFAULT_FRAME_INTERVAL_S,
) -> dict[str, Any]:
    """Convert pose dictionaries into the stable Lua-facing JSON schema."""
    interval_s = float(frame_interval_s)
    if not math.isfinite(interval_s) or interval_s <= 0:
        raise ValueError("frame_interval_s must be finite and positive")
    pose_list = list(poses)
    wheel_list = list(wheel_state) if wheel_state is not None else None
    if wheel_list is not None and len(wheel_list) != len(pose_list):
        raise ValueError("wheel_state must have one item per pose")
    frames: list[dict[str, Any]] = []
    for index, pose in enumerate(pose_list):
        position = _finite_vector(pose.get("positionM"), 3, "positionM")
        rotation = _finite_vector(pose.get("rotationRad"), 3, "rotationRad")
        frame: dict[str, Any] = {
            "timeS": float(pose.get("timeS", index * interval_s)),
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
        "frameIntervalS": interval_s,
        "frames": frames,
    }
    if lap_time_s is not None:
        if not math.isfinite(float(lap_time_s)) or float(lap_time_s) <= 0:
            raise ValueError("lap_time_s must be finite and positive")
        payload["lapTimeS"] = float(lap_time_s)
    return payload


def write_runtime_ghost(
    filename: str | Path,
    poses: Iterable[Mapping[str, Any]],
    *,
    car_id: str,
    track_id: str,
    wheel_state: Iterable[Mapping[str, Any]] | None = None,
    lap_time_s: float | None = None,
    frame_interval_s: float = DEFAULT_FRAME_INTERVAL_S,
) -> Path:
    """Write a deterministic UTF-8 runtime ghost file and return its path."""
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_runtime_ghost(
        poses,
        car_id=car_id,
        track_id=track_id,
        wheel_state=wheel_state,
        lap_time_s=lap_time_s,
        frame_interval_s=frame_interval_s,
    )
    path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n", encoding="utf-8")
    return path


def _align_poses_to_start_line(
    poses: Sequence[Mapping[str, Any]],
    start_line_xz: Sequence[float],
    frame_interval_s: float,
) -> tuple[list[dict[str, Any]], int]:
    """Circularly rotate poses so frame 0 is closest in XZ to ``start_line_xz``."""
    pose_list = [dict(p) for p in poses]
    if not pose_list:
        return pose_list, 0
    target = np.asarray(start_line_xz, dtype=np.float64)
    pose_xz = np.array(
        [[p["positionM"][0], p["positionM"][2]] for p in pose_list], dtype=np.float64
    )
    sf_idx = int(np.argmin(np.linalg.norm(pose_xz - target, axis=1)))
    shifted = pose_list[sf_idx:] + pose_list[:sf_idx]
    for idx, item in enumerate(shifted):
        item["timeS"] = round(idx * frame_interval_s, 6)
    return shifted, sf_idx


def _track_id_from_reference(track_ref: Mapping[str, Any]) -> str:
    track_info = track_ref.get("track") or {}
    name = str(track_info.get("name") or "").strip()
    layout = str(track_info.get("layout") or "").strip()
    if name and layout:
        return f"{name}/{layout}"
    return name or "unknown_track"


def export_runtime_ghost_from_acreplay(
    replay_path: str | Path,
    output_path: str | Path,
    *,
    car_index: int = 0,
    car_id: str | None = None,
    track_id: str | None = None,
    height_offset_m: float = 0.0,
    gps_track_path: str | Path | None = None,
    align_start_line: bool = False,
    smooth: bool = False,
    lap_time_s: float | None = None,
) -> dict[str, Any]:
    """Extract a ``ghost-runtime-v1.json`` timeline directly from an ``.acreplay``."""
    from .ac_track import load_track_package
    from .acreplay import parse_acreplay

    decoded = parse_acreplay(Path(replay_path), max_frames=0)
    cars = decoded.get("cars") or []
    if not cars:
        raise ValueError("Replay contains no cars")
    if car_index < 0 or car_index >= len(cars):
        raise ValueError(f"Car index {car_index} out of range (replay has {len(cars)} car(s))")

    car = cars[car_index]
    frames = car.get("frames") or []
    if len(frames) < 2:
        raise ValueError("Selected replay car contains fewer than 2 frames")

    header = decoded["header"]
    interval_ms = float(header.get("recordingIntervalMs") or 15.0)
    interval_s = interval_ms / 1000.0 if interval_ms > 0 else DEFAULT_FRAME_INTERVAL_S

    resolved_car_id = car_id or str(car.get("carID") or "unknown_car")
    track_name = str(header.get("track") or "").strip()
    track_config = str(header.get("trackConfig") or "").strip()
    default_track_id = f"{track_name}/{track_config}" if track_name and track_config else (track_name or "unknown_track")
    resolved_track_id = track_id or default_track_id

    offset_y = float(height_offset_m)
    poses: list[dict[str, Any]] = []
    for idx, fr in enumerate(frames):
        pos = list(fr["positionM"])
        pos[1] = float(pos[1]) + offset_y
        poses.append(
            {
                "timeS": round(idx * interval_s, 6),
                "positionM": [round(float(pos[0]), 4), round(float(pos[1]), 4), round(float(pos[2]), 4)],
                "rotationRad": [round(float(v), 6) for v in fr["rotationRad"]],
                "steerAngleDeg": round(float(fr.get("steerAngleDeg", 0.0)), 4),
                "velocityMS": [round(float(v), 4) for v in fr.get("velocityMS", [0.0, 0.0, 0.0])],
            }
        )

    sf_shift = 0
    if align_start_line and gps_track_path is not None:
        track_ref, _, _, _ = load_track_package(gps_track_path)
        start_line = track_ref.get("startLineAc")
        if start_line is not None and len(start_line) >= 3:
            poses, sf_shift = _align_poses_to_start_line(
                poses, [float(start_line[0]), float(start_line[2])], interval_s
            )

    resolved_lap_time_s = float(lap_time_s) if lap_time_s is not None else round(len(poses) * interval_s, 3)
    payload = build_runtime_ghost(
        poses,
        car_id=resolved_car_id,
        track_id=resolved_track_id,
        lap_time_s=resolved_lap_time_s,
        frame_interval_s=interval_s,
    )
    if smooth:
        payload = smooth_runtime_ghost(payload, lap_time_s=resolved_lap_time_s)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n", encoding="utf-8")
    return {
        "output": str(out),
        "sourceMode": "acreplay",
        "carId": resolved_car_id,
        "trackId": resolved_track_id,
        "frameCount": len(payload["frames"]),
        "frameIntervalS": interval_s,
        "lapTimeS": float(payload["lapTimeS"]),
        "smoothed": bool(smooth),
        "startLineShiftFrames": sf_shift,
    }


def export_runtime_ghost_from_ld(
    ld_path: str | Path,
    output_path: str | Path,
    *,
    gps_track_path: str | Path,
    car_id: str = "ig_toyota_gr86_premium",
    track_id: str | None = None,
    lap: int | None = None,
    channel_overrides: Mapping[str, str] | None = None,
    height_mode: str = "auto",
    height_offset_m: float | None = None,
    track_surface_paths: Sequence[str | Path] | None = None,
    position_smoothing_s: float = 0.75,
    yaw_smoothing_s: float = 0.35,
    fit_pavement: bool = True,
    pavement_search_radius_m: float = 8.0,
    align_start_line: bool = True,
    smooth: bool = True,
) -> dict[str, Any]:
    """Build and write a ``ghost-runtime-v1.json`` directly from a MoTeC ``.ld`` lap."""
    from .ac_track import load_track_package
    from .ld_replay import (
        align_replay_heights,
        build_poses_from_xyz,
        fit_pavement_offset,
        gps_track_to_ac,
        smooth_replay_positions,
    )
    from .motec import extract_motec_points
    from .track_surface import TrackSurface

    pts = extract_motec_points(
        Path(ld_path),
        target_lap=lap,
        channel_overrides=dict(channel_overrides) if channel_overrides else None,
    )
    track_ref, _, packaged_surfaces, _ = load_track_package(gps_track_path)
    resolved_track_id = track_id or _track_id_from_reference(track_ref)
    ref = np.asarray(track_ref["referencePathAc"], dtype=np.float64)

    surface_paths = [Path(p) for p in (track_surface_paths or packaged_surfaces)]
    has_surfaces = bool(surface_paths)
    effective_height_mode = ("kn5" if has_surfaces else "track") if height_mode == "auto" else height_mode
    if effective_height_mode == "kn5" and not has_surfaces:
        raise ValueError("--height-mode kn5 requires a packaged or explicit --track-surface")
    effective_height_offset_m = (
        float(height_offset_m)
        if height_offset_m is not None
        else (DEFAULT_KN5_HEIGHT_OFFSET_M if effective_height_mode == "kn5" else 0.0)
    )

    ac_xyz, rms_raw, rot = gps_track_to_ac(pts, track_ref)
    ac_xyz, _ = smooth_replay_positions(
        ac_xyz, frequency_hz=pts["frequencyHz"], window_s=float(position_smoothing_s)
    )

    pavement_diag: dict[str, Any] | None = None
    road_only = None
    if has_surfaces:
        road_only = TrackSurface.from_files([surface_paths[0]])
        if fit_pavement:
            ac_xyz, pavement_diag = fit_pavement_offset(
                ac_xyz,
                road_only,
                ref,
                search_radius_m=float(pavement_search_radius_m),
            )

    full_surface = TrackSurface.from_files(surface_paths) if effective_height_mode == "kn5" else None
    ac_xyz, height_diag = align_replay_heights(
        ac_xyz,
        ref,
        mode=effective_height_mode,
        offset_m=effective_height_offset_m,
        track_surface=full_surface,
    )

    poses, _ = build_poses_from_xyz(
        pts["points"], ac_xyz, rot, yaw_smoothing_s=float(yaw_smoothing_s)
    )

    sf_shift = 0
    if align_start_line:
        start_line = track_ref.get("startLineAc")
        if start_line is not None and len(start_line) >= 3:
            poses, sf_shift = _align_poses_to_start_line(
                poses, [float(start_line[0]), float(start_line[2])], DEFAULT_FRAME_INTERVAL_S
            )

    lap_time_s = round(float(pts["lapTimeS"]), 3)
    payload = build_runtime_ghost(
        poses,
        car_id=car_id,
        track_id=resolved_track_id,
        lap_time_s=lap_time_s,
    )
    if smooth:
        payload = smooth_runtime_ghost(payload, lap_time_s=lap_time_s)

    on_pavement_pct: float | None = None
    if road_only is not None and payload["frames"]:
        fpos = np.array([fr["positionM"] for fr in payload["frames"]], dtype=np.float64)
        query = fpos.copy()
        query[:, 1] -= effective_height_offset_m
        heights, _ = road_only.sample(query)
        on_pavement_pct = round(float(np.isfinite(heights).mean() * 100.0), 2)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n", encoding="utf-8")
    return {
        "output": str(out),
        "sourceMode": "ld",
        "selectedLap": pts.get("selectedLap"),
        "carId": car_id,
        "trackId": resolved_track_id,
        "frameCount": len(payload["frames"]),
        "frameIntervalS": DEFAULT_FRAME_INTERVAL_S,
        "lapTimeS": lap_time_s,
        "alignmentRmsRawM": round(float(rms_raw), 3),
        "heightMode": effective_height_mode,
        "heightOffsetM": effective_height_offset_m,
        "heightAlignment": height_diag,
        "pavementFit": pavement_diag,
        "onPavementPct": on_pavement_pct,
        "smoothed": bool(smooth),
        "startLineShiftFrames": sf_shift,
    }
