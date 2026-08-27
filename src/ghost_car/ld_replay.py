"""Convert a MoTeC LD lap into an Assetto Corsa .acreplay.

Pipeline:
1. Extract the selected LD lap via ghost_car.motec (position, speed,
   rpm, gear, accelerator pedal, brake, steering, pitch, roll).
2. Map the lap into AC world coordinates either by rigid 2D fitting
   onto the template replay's driven path (--gps-track absent), or
   directly through a track reference (--gps-track track.json) whose
   origin + ENU->AC matrix was calibrated on the same LD/track.
3. Resample to the replay cadence (15 ms).
4. Derive wrapped yaw from the fitted path (AC convention
   forward = (-sin yaw, cos yaw)), scale pedals, and morph the car.

The horizontal line comes from telemetry. By default, vertical positions
are projected onto an Assetto Corsa reference path because GPS altitude and
the simulator's world-height datum are independent.
"""
from __future__ import annotations

__all__ = [
    "align_replay_heights",
    "build_poses_from_xyz",
    "convert_ld_to_acreplay",
    "gps_track_to_ac",
    "offset_track_calibration",
    "rigid_fit_2d",
    "smooth_replay_positions",
    "validate_track_reference",
]

import math
import tempfile
from copy import deepcopy
from pathlib import Path

import numpy as np

from .ac_track import load_track_package
from .acreplay import parse_acreplay
from .motec import extract_motec_points
from .replay_writer import morph, replicate_car, resample
from .track_surface import TrackSurface

RECORDING_INTERVAL_MS = 15.0


def offset_track_calibration(track_ref, x_m=0.0, z_m=0.0):
    """Return a copy with a manual AC-world X/Z translation correction.

    The simulator reference path remains unchanged. Only the GPS-to-AC
    transform moves, preserving one shared correction across every source lap.
    """
    adjusted = deepcopy(track_ref)
    matrix = np.asarray(adjusted.get("enuToAc", {}).get("matrix"), dtype=float)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError("Track calibration needs a finite 4x4 enuToAc matrix")
    x_m = float(x_m)
    z_m = float(z_m)
    if not math.isfinite(x_m) or not math.isfinite(z_m):
        raise ValueError("Track calibration offsets must be finite")
    matrix[0, 3] += x_m
    matrix[2, 3] += z_m
    adjusted["enuToAc"]["matrix"] = matrix.tolist()
    calibration = adjusted.setdefault("calibration", {})
    previous = calibration.get("manualOffsetAcM", {})
    calibration["manualOffsetAcM"] = {
        "x": float(previous.get("x", 0.0)) + x_m,
        "z": float(previous.get("z", 0.0)) + z_m,
    }
    return adjusted


def _savgol_series(values, window):
    """Zero-phase quadratic Savitzky-Golay smoothing without SciPy."""
    values = np.asarray(values, dtype=np.float64)
    if len(values) < 3 or window < 3:
        return values.copy()
    window = min(int(window), len(values))
    if window % 2 == 0:
        window -= 1
    if window < 3:
        return values.copy()
    half = window // 2
    coordinates = np.arange(-half, half + 1, dtype=np.float64)
    design = np.vander(coordinates, 3, increasing=True)
    weights = np.linalg.pinv(design)[0]
    padded = np.pad(values, half, mode="edge")
    result = np.convolve(padded, weights[::-1], mode="valid")
    # Match SciPy's interpolation-style edge handling: fit the first/last
    # real window instead of repeating an endpoint, which otherwise moves
    # the start and finish of a fast lap by nearly a metre.
    edge_coordinates = np.arange(window, dtype=np.float64)
    edge_design = np.vander(edge_coordinates, 3, increasing=True)
    left_coefficients = np.linalg.lstsq(
        edge_design, values[:window], rcond=None
    )[0]
    right_coefficients = np.linalg.lstsq(
        edge_design, values[-window:], rcond=None
    )[0]
    result[:half] = edge_design[:half] @ left_coefficients
    result[-half:] = edge_design[-half:] @ right_coefficients
    return result


def _smoothing_window(frequency_hz, window_s, point_count):
    if window_s <= 0.0 or frequency_hz <= 0.0 or point_count < 3:
        return 1
    window = max(3, int(round(float(frequency_hz) * float(window_s))))
    if window % 2 == 0:
        window += 1
    if window > point_count:
        window = point_count if point_count % 2 else point_count - 1
    return max(1, window)


def _control_fraction(values):
    """Map common fraction, percent, or byte control ranges into [0, 1]."""
    values = np.asarray(values, dtype=np.float64)
    peak = float(np.percentile(np.abs(values), 99.5)) if len(values) else 0.0
    if peak <= 1.5:
        scale = 1.0
    elif peak <= 110.0:
        scale = 100.0
    elif peak <= 280.0:
        scale = 255.0
    else:
        scale = max(1.0, peak)
    return np.clip(values / scale, 0.0, 1.0)


def _accelerator_fraction(values):
    """Normalize a driver-pedal sensor and remove its released-position bias.

    Some pedal channels report a stable physical rest position above zero.
    Detect the modal value in the bottom half of the range, but only accept it
    as a release offset when it is at most 15% of full scale. A further 2%
    dead zone absorbs rest-position noise without mistaking a sustained light
    throttle value for the sensor zero.
    """
    fraction = _control_fraction(values)
    low = fraction[fraction <= 0.5]
    minimum_samples = max(5, int(math.ceil(len(fraction) * 0.02)))
    release = 0.0
    if len(low) >= minimum_samples:
        counts, edges = np.histogram(low, bins=np.linspace(0.0, 0.5, 51))
        mode = int(np.argmax(counts))
        modal = low[(low >= edges[mode]) & (low <= edges[mode + 1])]
        candidate = float(np.median(modal)) if len(modal) else 0.0
        if candidate <= 0.15:
            release = candidate
    threshold = min(0.25, release + 0.02)
    return np.clip((fraction - threshold) / (1.0 - threshold), 0.0, 1.0)


def smooth_replay_positions(positions, frequency_hz, window_s=0.75):
    """Remove GPS X/Z jitter while preserving the vertical profile."""
    positions = np.asarray(positions, dtype=np.float64)
    if positions.ndim != 2 or positions.shape[1] != 3:
        raise ValueError("Replay positions must be an Nx3 array")
    window = _smoothing_window(frequency_hz, window_s, len(positions))
    smoothed = positions.copy()
    if window >= 3:
        smoothed[:, 0] = _savgol_series(positions[:, 0], window)
        smoothed[:, 2] = _savgol_series(positions[:, 2], window)
    shifts = np.linalg.norm(
        smoothed[:, [0, 2]] - positions[:, [0, 2]], axis=1
    )
    diagnostics = {
        "positionSmoothingS": float(window_s),
        "positionSmoothingMode": "world-xz",
        "positionSmoothingSamples": int(window),
        "positionSmoothingRmsM": float(np.sqrt(np.mean(shifts ** 2))),
        "positionSmoothingP95M": float(np.percentile(shifts, 95)),
        "positionSmoothingMaxM": float(np.max(shifts)),
    }
    return smoothed, diagnostics


def rigid_fit_2d(source, target):
    """Rigid fit (rotation + translation, optional reflection) of source
    points onto target points; returns (R, t, rms).  Source is resampled
    to the target point count by index interpolation first."""
    source = np.asarray(source, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    if len(source) != len(target):
        indices = np.linspace(0, len(source) - 1, len(target))
        source = np.column_stack(
            [np.interp(indices, np.arange(len(source)), source[:, column])
             for column in range(source.shape[1])]
        )
    source_mean = source.mean(axis=0)
    target_mean = target.mean(axis=0)
    source_centered = source - source_mean
    target_centered = target - target_mean
    covariance = target_centered.T @ source_centered
    u, _, vt = np.linalg.svd(covariance)
    d = np.sign(np.linalg.det(vt.T @ u.T))
    correction = np.diag([1.0, d])
    rotation = u @ correction @ vt
    translation = target_mean - source_mean @ rotation.T
    fitted = source @ rotation.T + translation
    rms = float(np.sqrt(((fitted - target) ** 2).sum() / len(target)))
    return rotation, translation, rms


def gps_track_to_ac(points, track_ref):
    """Map LD GPS samples to AC world coordinates via a track reference.

    ``points`` carries xM/yM/zM in a local ENU frame anchored at the LD's
    first sample; ``track_ref`` is a track.json with an origin and an
    ENU->AC 4x4 matrix.  Returns (Nx3 AC coords, alignment rms m,
    ENU->AC rotation 2x2 for heading mapping).
    """
    earth_radius = 6371008.8
    origin = points["origin"]
    track_origin = track_ref["origin"]
    lat0 = math.radians(float(origin["latitudeDeg"]))
    cos_lat = math.cos(lat0)
    east_offset = (
        math.radians(float(origin["longitudeDeg"]) - float(track_origin["longitudeDeg"]))
        * earth_radius
        * cos_lat
    )
    north_offset = (
        math.radians(float(origin["latitudeDeg"]) - float(track_origin["latitudeDeg"]))
        * earth_radius
    )
    up_offset = float(origin["altitudeM"]) - float(track_origin["altitudeM"])
    matrix = np.array(track_ref["enuToAc"]["matrix"], dtype=np.float64)
    ac = []
    for point in points["points"]:
        enu = np.array(
            [
                point["xM"] + east_offset,
                point["yM"] + north_offset,
                point["zM"] + up_offset,
                1.0,
            ]
        )
        ac.append((matrix @ enu)[:3])
    ac = np.asarray(ac)
    reference = np.array(track_ref["referencePathAc"], dtype=np.float64)
    delta = ac[:, None, [0, 2]] - reference[None, :, [0, 2]]
    nearest = np.sqrt(np.min(np.einsum("qri,qri->qr", delta, delta), axis=1))
    rms = float(np.sqrt((nearest ** 2).mean()))
    # The 4x4 matrix maps (e, n, u) to (x, y, z); the horizontal
    # rotation is rows 0 and 2, columns 0 and 1.
    return ac, rms, matrix[[0, 2], :2]


def _project_reference_segments(reference_xyz, query_xyz):
    """Project X/Z queries onto all reference segments in one vectorized pass."""
    reference = np.asarray(reference_xyz, dtype=np.float64)
    query = np.asarray(query_xyz, dtype=np.float64)
    if reference.ndim != 2 or reference.shape[1] != 3 or len(reference) < 2:
        raise ValueError("Reference needs at least two XYZ points")
    if query.ndim != 2 or query.shape[1] != 3:
        raise ValueError("Queries need an Nx3 array")
    if not np.allclose(reference[0], reference[-1]):
        reference = np.vstack((reference, reference[0]))
    start = reference[:-1]
    delta = reference[1:] - start
    horizontal = delta[:, [0, 2]]
    length_sq = np.einsum("ij,ij->i", horizontal, horizontal)
    valid = length_sq > 1e-12
    relative = query[:, None, [0, 2]] - start[None, :, [0, 2]]
    along = np.zeros((len(query), len(start)), dtype=np.float64)
    along[:, valid] = np.einsum(
        "qsi,si->qs", relative[:, valid], horizontal[valid]
    ) / length_sq[valid]
    along = np.clip(along, 0.0, 1.0)
    projected = start[None, :, [0, 2]] + along[:, :, None] * horizontal[None, :, :]
    distance_sq = np.einsum(
        "qsi,qsi->qs", query[:, None, [0, 2]] - projected,
        query[:, None, [0, 2]] - projected,
    )
    segment = np.argmin(distance_sq, axis=1)
    rows = np.arange(len(query))
    return segment, along[rows, segment], np.sqrt(distance_sq[rows, segment])


def _reference_heights(reference_xyz, query_xyz):
    """Interpolate reference Y at each query's nearest X/Z path segment."""
    reference = np.asarray(reference_xyz, dtype=np.float64)
    query = np.asarray(query_xyz, dtype=np.float64)
    if reference.ndim != 2 or reference.shape[1] != 3 or len(reference) < 2:
        raise ValueError("Height reference needs at least two XYZ points")
    if not np.allclose(reference[0], reference[-1]):
        reference = np.vstack((reference, reference[0]))
    segment, along, horizontal_distance = _project_reference_segments(
        reference, query
    )
    result = reference[segment, 1] + along * (
        np.asarray(reference)[np.minimum(segment + 1, len(reference) - 1), 1]
        - reference[segment, 1]
    )
    return result, horizontal_distance


def align_replay_heights(
    mapped_xyz,
    reference_xyz,
    mode="track",
    offset_m=0.0,
    track_surface=None,
):
    """Align telemetry heights to the simulator reference path.

    Track mode replaces GPS altitude with the nearest AC reference-segment
    height. GPS-offset mode preserves the GPS elevation profile but removes
    its median datum offset. GPS mode keeps the mapped GPS height unchanged.
    The offset is applied last for small car/body corrections.
    """
    mapped = np.asarray(mapped_xyz, dtype=np.float64)
    if mapped.ndim != 2 or mapped.shape[1] != 3:
        raise ValueError("Mapped replay positions must be an Nx3 array")
    if mode not in ("track", "kn5", "gps-offset", "gps"):
        raise ValueError("Unknown height mode: {}".format(mode))
    if mode == "kn5" and track_surface is None:
        raise ValueError("KN5 height mode requires a track surface")
    reference_y, horizontal_distance = _reference_heights(reference_xyz, mapped)
    before = mapped[:, 1] - reference_y
    aligned = mapped.copy()
    datum_offset = float(np.median(before))
    surface_hits = None
    surface_clearance = None
    if mode == "kn5":
        reference_surface_y, _ = track_surface.sample(reference_xyz)
        reference_hits = np.isfinite(reference_surface_y)
        if np.count_nonzero(reference_hits) < 2:
            raise ValueError("KN5 surface does not overlap the AC reference path")
        surface_clearance = float(
            np.median(
                np.asarray(reference_xyz)[reference_hits, 1]
                - reference_surface_y[reference_hits]
            )
        )
        surface_y, _ = track_surface.sample(mapped)
        surface_hits = np.isfinite(surface_y)
        aligned[:, 1] = reference_y
        aligned[surface_hits, 1] = (
            surface_y[surface_hits] + surface_clearance
        )
    elif mode == "track":
        aligned[:, 1] = reference_y
    elif mode == "gps-offset":
        aligned[:, 1] -= datum_offset
    aligned[:, 1] += float(offset_m)
    after = aligned[:, 1] - reference_y
    diagnostics = {
        "heightMode": mode,
        "heightOffsetM": float(offset_m),
        "verticalDatumOffsetM": datum_offset,
        "beforeVerticalRmseM": float(np.sqrt(np.mean(before ** 2))),
        "afterVerticalRmseM": float(np.sqrt(np.mean(after ** 2))),
        "afterVerticalP95M": float(np.percentile(np.abs(after), 95)),
        "horizontalReferenceRmseM": float(
            np.sqrt(np.mean(horizontal_distance ** 2))
        ),
    }
    if surface_hits is not None:
        diagnostics.update(
            {
                "surfaceMatchedPoints": int(np.count_nonzero(surface_hits)),
                "surfaceTotalPoints": int(len(surface_hits)),
                "surfaceMatchRatio": float(np.mean(surface_hits)),
                "surfaceBodyClearanceM": surface_clearance,
            }
        )
    return aligned, diagnostics


def _reference_track_progress(reference_xyz, query_xyz):
    """Project query points onto one closed reference and unwrap its station."""
    reference = np.asarray(reference_xyz, dtype=np.float64)
    query = np.asarray(query_xyz, dtype=np.float64)
    if reference.ndim != 2 or reference.shape[1] != 3 or len(reference) < 3:
        raise ValueError("Track-progress reference needs at least three XYZ points")
    if not np.allclose(reference[0], reference[-1]):
        reference = np.vstack((reference, reference[0]))
    start = reference[:-1]
    horizontal = reference[1:, [0, 2]] - start[:, [0, 2]]
    lengths = np.linalg.norm(horizontal, axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(lengths)))
    total_length = float(cumulative[-1])
    if total_length <= 1e-6:
        raise ValueError("Track-progress reference has no horizontal length")
    segment, along, _ = _project_reference_segments(reference, query)
    wrapped = cumulative[segment] + along * lengths[segment]
    unwrapped = np.unwrap(wrapped * 2.0 * math.pi / total_length)
    unwrapped *= total_length / (2.0 * math.pi)
    differences = np.diff(unwrapped)
    moving = differences[np.abs(differences) > 1e-6]
    direction = -1.0 if len(moving) and np.median(moving) < 0.0 else 1.0
    progress = direction * (unwrapped - unwrapped[0])
    return np.maximum.accumulate(progress), total_length


def _synchronize_lap_progress(
    points, fitted_xyz, duration_s, reference_xyz=None
):
    """Retimestamp one lap by horizontal path progress for car comparison.

    Every compared car reaches the same normalized progress at the same replay
    time.  The original spatial samples and controls are retained; only
    duplicate stationary samples are removed and time/speed/lap metadata are
    replaced.  The path is deliberately not closed between its last and first
    samples.
    """
    xyz = np.asarray(fitted_xyz, dtype=np.float64)
    if xyz.ndim != 2 or xyz.shape[1] != 3 or len(xyz) != len(points):
        raise ValueError("Comparison positions must match the telemetry points")
    if len(points) < 2:
        raise ValueError("A comparison lap requires at least two points")
    if duration_s <= 0.0:
        raise ValueError("Comparison duration must be positive")
    steps = np.linalg.norm(np.diff(xyz[:, [0, 2]], axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(steps)))
    path_length = float(cumulative[-1])
    if not math.isfinite(path_length) or path_length <= 1e-6:
        raise ValueError("Comparison lap has no horizontal path length")
    if reference_xyz is None:
        synchronization_progress = cumulative
        synchronization_method = "driven-distance"
    else:
        synchronization_progress, _ = _reference_track_progress(
            reference_xyz, xyz
        )
        synchronization_method = "track-progress"
    progress_span = float(synchronization_progress[-1])
    if not math.isfinite(progress_span) or progress_span <= 1e-6:
        raise ValueError("Comparison lap has no forward track progress")
    keep = np.concatenate(
        ([True], np.diff(synchronization_progress) > 1e-6)
    )
    kept_progress = synchronization_progress[keep]
    synchronized = []
    constant_speed = path_length / float(duration_s)
    for point, progress in zip(np.asarray(points, dtype=object)[keep], kept_progress):
        item = dict(point)
        item["timeS"] = float(progress / progress_span * duration_s)
        item["speedMS"] = constant_speed
        item["lapNumber"] = 1
        synchronized.append(item)
    return synchronized, xyz[keep], {
        "comparisonPathLengthM": path_length,
        "comparisonDurationS": float(duration_s),
        "comparisonSynchronization": synchronization_method,
    }


def _pad_comparison_poses(poses, frame_count):
    """Hold a finished comparison car at its final pose."""
    if not poses:
        raise ValueError("Comparison car has no poses")
    if frame_count < len(poses):
        raise ValueError("Comparison frame count cannot truncate a car")
    result = [deepcopy(pose) for pose in poses]
    if len(result) == frame_count:
        return result
    held = deepcopy(result[-1])
    held["velocityMS"] = [0.0, 0.0, 0.0]
    held["gas"] = 0
    held["brake"] = 0
    result.extend(deepcopy(held) for _ in range(frame_count - len(result)))
    return result


def build_poses_from_xyz(
    points, fitted_xyz, rotation=None, yaw_smoothing_s=0.75
):
    """Resample the aligned LD points to the 15 ms replay grid.

    Yaw is derived from the LD GPS heading channel (mapped through the
    fit rotation) when available, else from the smoothed fitted path.
    Pitch is derived from the aligned 3D path because some LD exports have
    no useful body-pitch channel.
    """
    fitted_xyz = np.asarray(fitted_xyz, dtype=np.float64)
    duration = points[-1]["timeS"]
    interval_s = RECORDING_INTERVAL_MS / 1000.0
    frame_count = max(2, int(math.floor(duration / interval_s)) + 1)
    times = np.array([point["timeS"] for point in points], dtype=np.float64)
    grid = np.arange(frame_count) * interval_s

    def series(key, default=0.0):
        values = np.array(
            [point.get(key, default) if point.get(key) is not None else default
             for point in points],
            dtype=np.float64,
        )
        finite = np.isfinite(values)
        if not np.any(finite):
            return np.full_like(grid, float(default), dtype=np.float64)
        return np.interp(grid, times[finite], values[finite])

    fitted_x = np.interp(grid, times, fitted_xyz[:, 0])
    fitted_y = np.interp(grid, times, fitted_xyz[:, 1])
    fitted_z = np.interp(grid, times, fitted_xyz[:, 2])
    speed = series("speedMS")
    path_dx = np.gradient(fitted_x)
    path_dy = np.gradient(fitted_y)
    path_dz = np.gradient(fitted_z)
    pitch = np.arctan2(path_dy, np.hypot(path_dx, path_dz))
    pitch_window = _smoothing_window(
        1.0 / interval_s, yaw_smoothing_s, len(pitch)
    )
    pitch = _savgol_series(pitch, pitch_window)
    roll = series("rollRad")
    rpm = series("rpm")
    accelerator = _accelerator_fraction(series("throttle"))
    brake = _control_fraction(series("brake"))
    steer = series("steerRad")
    gear_raw = series("gear", default=0.0)
    source_laps = np.asarray(
        [point.get("lapNumber", 1) for point in points], dtype=np.float64
    )
    source_laps = np.nan_to_num(source_laps, nan=1.0)
    source_indices = np.searchsorted(times, grid, side="right") - 1
    source_indices = np.clip(source_indices, 0, len(source_laps) - 1)
    lap_number = np.maximum(1, np.rint(source_laps[source_indices]).astype(int))
    lap_elapsed_ms = np.zeros(frame_count, dtype=np.int64)
    lap_start_s = float(grid[0])
    for index in range(frame_count):
        if index == 0 or lap_number[index] != lap_number[index - 1]:
            lap_start_s = float(grid[index])
        lap_elapsed_ms[index] = int(round((grid[index] - lap_start_s) * 1000.0))

    # Yaw: GPS heading (0 = north, clockwise, in the ENU frame) mapped
    # through the fit rotation into the AC convention; fall back to the
    # path direction.  Work on the RAW samples: unwrap at the native
    # cadence, smooth, and only then interpolate to the replay grid.
    # Interpolating a wrapped angle across its +-pi seam would swing the
    # signal through zero and destroy the lap sweep.
    has_heading = any(point.get("headingRad") is not None for point in points)
    if has_heading and rotation is not None:
        heading_raw = np.array(
            [point["headingRad"] for point in points], dtype=np.float64
        )
        east_raw = rotation[0, 0] * np.sin(heading_raw) + rotation[0, 1] * np.cos(heading_raw)
        north_raw = rotation[1, 0] * np.sin(heading_raw) + rotation[1, 1] * np.cos(heading_raw)
        # Reject isolated heading-channel glitches in unit-vector space so
        # the 0/2pi seam cannot confuse a scalar median.
        if len(east_raw) >= 3:
            east_padded = np.pad(east_raw, 1, mode="edge")
            north_padded = np.pad(north_raw, 1, mode="edge")
            east_raw = np.median(
                np.vstack((east_padded[:-2], east_padded[1:-1], east_padded[2:])),
                axis=0,
            )
            north_raw = np.median(
                np.vstack(
                    (north_padded[:-2], north_padded[1:-1], north_padded[2:])
                ),
                axis=0,
            )
        yaw_raw = np.unwrap(np.arctan2(-east_raw, north_raw))
    else:
        path_raw = np.asarray(fitted_xyz, dtype=np.float64)
        dx_raw = np.gradient(path_raw[:, 0])
        dz_raw = np.gradient(path_raw[:, 2])
        yaw_raw = np.unwrap(np.arctan2(-dx_raw, dz_raw))
    native_interval = float(np.median(np.diff(times))) if len(times) > 1 else 0.0
    native_frequency = 1.0 / native_interval if native_interval > 0.0 else 0.0
    yaw_window = _smoothing_window(
        native_frequency, yaw_smoothing_s, len(yaw_raw)
    )
    yaw_raw = _savgol_series(yaw_raw, yaw_window)
    yaw = np.interp(grid, times, yaw_raw)
    yaw = (yaw + math.pi) % (2.0 * math.pi) - math.pi

    poses = []
    for index in range(frame_count):
        poses.append(
            {
                "positionM": [
                    float(fitted_x[index]),
                    float(fitted_y[index]),
                    float(fitted_z[index]),
                ],
                "rotationRad": [float(pitch[index]), float(yaw[index]), float(roll[index])],
                "velocityMS": [
                    float(speed[index] * -math.sin(yaw[index])),
                    0.0,
                    float(speed[index] * math.cos(yaw[index])),
                ],
                "rpm": float(rpm[index]),
                # iRacing LD gears are -1=R, 0=N, 1..n; AC stores 0=R, 1=N, 2=1st.
                "gear": max(0, min(8, int(round(gear_raw[index])) + 1)),
                # AC's gas byte is driver accelerator input, not the engine's
                # electronic throttle-plate position.
                "gas": int(round(255.0 * accelerator[index])),
                "brake": int(round(255.0 * brake[index])),
                "steerAngleDeg": float(math.degrees(steer[index])),
                # LD does not provide trustworthy per-wheel slip. Retaining
                # unrelated template values can trigger false skid smoke.
                "slipAngleRad": [0.0] * 4,
                "slipRatio": [0.0] * 4,
                "ndSlip": [0.0] * 4,
                "currentLap": min(255, int(lap_number[index] - 1)),
                "currentLapTimeMs": int(lap_elapsed_ms[index]),
                "lapBoundaryPulse": bool(
                    index > 0 and lap_number[index] != lap_number[index - 1]
                ),
            }
        )
    return poses, frame_count

def validate_track_reference(replay, track_ref):
    track = track_ref.get("track", {})
    expected_name = track.get("name")
    expected_layout = track.get("layout")
    actual_name = replay["header"]["track"]
    actual_layout = replay["header"]["trackConfig"]
    if expected_name and expected_name != actual_name:
        raise ValueError(
            "Track reference is for {!r}, but template is {!r}".format(
                expected_name, actual_name
            )
        )
    if expected_layout and expected_layout != actual_layout:
        raise ValueError(
            "Track reference layout is {!r}, but template layout is {!r}".format(
                expected_layout, actual_layout
            )
        )


def convert_ld_to_acreplay(
    template_path,
    ld_path,
    output_path,
    car_index=0,
    lap=None,
    session=False,
    compare_laps=None,
    compare_skins=None,
    compare_sync="time",
    channel_overrides=None,
    gps_track_path=None,
    height_mode="track",
    height_offset_m=0.0,
    position_smoothing_s=1.0,
    yaw_smoothing_s=1.0,
    wheel_steer_multiplier=1.0,
    track_surface_path=None,
):
    """Convert one lap, a full session, or synchronized comparison laps."""
    compared_laps = None if compare_laps is None else [int(item) for item in compare_laps]
    if compare_sync not in ("time", "progress", "driven-distance"):
        raise ValueError("Unknown comparison timing: {}".format(compare_sync))
    selected_modes = int(bool(session)) + int(compared_laps is not None) + int(lap is not None)
    if selected_modes > 1:
        raise ValueError("--lap, --session, and --compare-laps are mutually exclusive")
    if compared_laps is not None:
        if len(compared_laps) < 2:
            raise ValueError("--compare-laps requires at least two lap numbers")
        if len(compared_laps) > 16:
            raise ValueError("At most 16 comparison laps are supported")
        if any(item <= 0 for item in compared_laps):
            raise ValueError("Comparison lap numbers must be positive")
        if len(set(compared_laps)) != len(compared_laps):
            raise ValueError("Comparison lap numbers must be distinct")
        if not gps_track_path:
            raise ValueError(
                "--compare-laps requires --gps-track for GPS-to-AC mapping; "
                "driven-distance only avoids the shared reference station"
            )
    if compare_skins is not None and compared_laps is None:
        raise ValueError("--compare-skins can only be used with --compare-laps")
    if compare_skins is not None and len(compare_skins) != len(compared_laps):
        raise ValueError("Comparison skin count must match comparison lap count")

    overrides = channel_overrides or {}
    if compared_laps is not None:
        point_sets = [
            extract_motec_points(
                ld_path,
                target_lap=lap_number,
                channel_overrides=overrides,
            )
            for lap_number in compared_laps
        ]
        mode = "compare"
    elif session:
        point_sets = [
            extract_motec_points(
                ld_path,
                lap_selection="all",
                channel_overrides=overrides,
            )
        ]
        mode = "session"
    else:
        point_sets = [
            extract_motec_points(
                ld_path,
                target_lap=lap,
                channel_overrides=overrides,
            )
        ]
        mode = "lap"

    template_path = Path(template_path).expanduser()
    replay = parse_acreplay(template_path, max_frames=0)
    if car_index < 0 or car_index >= len(replay["cars"]):
        raise ValueError("Car index {} out of range".format(car_index))
    if compared_laps is not None and (len(replay["cars"]) != 1 or car_index != 0):
        raise ValueError("Lap comparison currently requires a single-car template")
    car = replay["cars"][car_index]
    template_xyz = np.array([frame["positionM"] for frame in car["frames"]])

    track_ref = None
    packaged_surface_paths = []
    if gps_track_path:
        track_ref, _, packaged_surface_paths, _ = load_track_package(gps_track_path)
        validate_track_reference(replay, track_ref)
    track_surface = None
    requested_surface_paths = track_surface_path
    if requested_surface_paths is None and height_mode == "kn5":
        requested_surface_paths = packaged_surface_paths
    if requested_surface_paths:
        if track_ref is None:
            raise ValueError("--track-surface requires --gps-track")
        surface_paths = (
            [requested_surface_paths]
            if isinstance(requested_surface_paths, (str, Path))
            else list(requested_surface_paths)
        )
        missing = [path for path in surface_paths if not Path(path).is_file()]
        if missing:
            raise ValueError(
                "Track surface does not exist: {}. Generate it locally with "
                "'ghost-car replay export-kn5-surface' or pass --track-surface."
                .format(missing[0])
            )
        track_surface = TrackSurface.from_files(surface_paths)

    aligned_sets = []
    for points in point_sets:
        if track_ref is not None:
            transformed, rms, rotation = gps_track_to_ac(points, track_ref)
            reference = np.asarray(track_ref["referencePathAc"], dtype=np.float64)
            alignment_method = "gps-track"
        else:
            source_xy = np.array(
                [[point["xM"], point["yM"]] for point in points["points"]],
                dtype=np.float64,
            )
            target_xz = template_xyz[::10, [0, 2]]
            rotation, translation, rms = rigid_fit_2d(source_xy, target_xz)
            transformed = np.array(
                [
                    [
                        point["xM"] * rotation[0, 0]
                        + point["yM"] * rotation[0, 1]
                        + translation[0],
                        point["zM"],
                        point["xM"] * rotation[1, 0]
                        + point["yM"] * rotation[1, 1]
                        + translation[1],
                    ]
                    for point in points["points"]
                ]
            )
            reference = template_xyz
            alignment_method = "template-rigid-fit"

        transformed, smoothing_diagnostics = smooth_replay_positions(
            transformed,
            frequency_hz=points["frequencyHz"],
            window_s=position_smoothing_s,
        )
        transformed, height_diagnostics = align_replay_heights(
            transformed,
            reference,
            mode=height_mode,
            offset_m=height_offset_m,
            track_surface=track_surface,
        )
        aligned_sets.append(
            {
                "source": points,
                "xyz": transformed,
                "rotation": rotation,
                "diagnostics": {
                    "alignmentMethod": alignment_method,
                    "horizontalAlignmentRmseM": float(rms),
                    **smoothing_diagnostics,
                    **height_diagnostics,
                },
            }
        )

    if compared_laps is not None:
        durations = [item["source"]["lapTimeS"] for item in aligned_sets]
        replay_duration = min(durations) if compare_sync in ("progress", "driven-distance") else max(durations)
        poses_by_car = {}
        per_car = []
        built_poses = []
        built_counts = []
        progress_by_car = []
        for item in aligned_sets:
            if compare_sync in ("progress", "driven-distance"):
                source_points, source_xyz, progress_diagnostics = (
                    _synchronize_lap_progress(
                        item["source"]["points"],
                        item["xyz"],
                        replay_duration,
                        reference_xyz=(
                            reference if compare_sync == "progress" else None
                        ),
                    )
                )
            else:
                source_points = item["source"]["points"]
                source_xyz = item["xyz"]
                progress_diagnostics = {
                    "comparisonDurationS": float(replay_duration),
                    "comparisonSynchronization": "actual-time",
                }
            poses, current_frame_count = build_poses_from_xyz(
                source_points,
                source_xyz,
                item["rotation"],
                yaw_smoothing_s=yaw_smoothing_s,
            )
            built_poses.append(poses)
            built_counts.append(current_frame_count)
            progress_by_car.append(progress_diagnostics)
        frame_count = max(built_counts)
        if compare_sync == "progress" and len(set(built_counts)) != 1:
            raise ValueError("Comparison cars produced different replay lengths")
        for index, item in enumerate(aligned_sets):
            poses_by_car[index] = _pad_comparison_poses(
                built_poses[index], frame_count
            )
            per_car.append(
                {
                    "carIndex": index,
                    "selectedLap": compared_laps[index],
                    "lapTimeS": item["source"]["lapTimeS"],
                    "finishFrame": built_counts[index] - 1,
                    "sourcePointCount": len(item["source"]["points"]),
                    **item["diagnostics"],
                    **progress_by_car[index],
                }
            )
    else:
        item = aligned_sets[0]
        poses, frame_count = build_poses_from_xyz(
            item["source"]["points"],
            item["xyz"],
            item["rotation"],
            yaw_smoothing_s=yaw_smoothing_s,
        )
        poses_by_car = None
        per_car = []
        replay_duration = item["source"]["lapTimeS"]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        working_template = template_path
        if frame_count != car["numFrames"]:
            resampled = str(tmp_dir / "template_resampled.acreplay")
            resample(working_template, resampled, frame_count)
            working_template = Path(resampled)
        if compared_laps is not None:
            replicated = tmp_dir / "template_multicar.acreplay"
            replicate_car(
                working_template,
                replicated,
                driver_names=["Lap {}".format(item) for item in compared_laps],
                skin_ids=compare_skins,
            )
            morph(
                replicated,
                output_path,
                poses_by_car=poses_by_car,
                wheel_steer_multiplier=wheel_steer_multiplier,
            )
        else:
            morph(
                working_template,
                output_path,
                car_index=car_index,
                poses=poses,
                wheel_steer_multiplier=wheel_steer_multiplier,
            )

    check = parse_acreplay(output_path, max_frames=2)
    first_source = point_sets[0]
    common_diagnostics = aligned_sets[0]["diagnostics"]
    return {
        "output": str(output_path),
        "fileSizeBytes": check["fileSizeBytes"],
        "frameCount": check["header"]["numFrames"],
        "mode": mode,
        "carCount": len(check["cars"]),
        "carIndex": car_index,
        "driverName": check["cars"][car_index]["driverName"],
        "driverNames": [item["driverName"] for item in check["cars"]],
        "selectedLap": first_source["selectedLap"] if mode == "lap" else None,
        "selectedLaps": compared_laps,
        "compareSync": compare_sync if mode == "compare" else None,
        "lapTimeS": float(replay_duration),
        "sourcePointCount": sum(len(item["points"]) for item in point_sets),
        "sourceFrequencyHz": first_source["frequencyHz"],
        "sourceTrack": first_source["metadata"]["trackName"],
        "sourceCar": first_source["metadata"]["carName"],
        "wheelSteerMultiplier": float(wheel_steer_multiplier),
        "sessionSegments": first_source.get("sessionSegments", [])
        if mode == "session"
        else [],
        "perCar": per_car,
        **common_diagnostics,
    }
