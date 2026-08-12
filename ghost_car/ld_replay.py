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

__all__ = [
    "align_replay_heights",
    "build_poses_from_xyz",
    "convert_ld_to_acreplay",
    "gps_track_to_ac",
    "rigid_fit_2d",
    "smooth_replay_positions",
    "validate_track_reference",
]

import json
import math
import tempfile
from pathlib import Path

import numpy as np

from .acreplay import parse_acreplay
from .motec import extract_motec_points
from .replay_writer import morph, resample

RECORDING_INTERVAL_MS = 15.0


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
    """Remove GPS-scale X/Z jitter while preserving the vertical profile."""
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
        math.radians(float(track_origin["longitudeDeg"]) - float(origin["longitudeDeg"]))
        * earth_radius
        * cos_lat
    )
    north_offset = (
        math.radians(float(track_origin["latitudeDeg"]) - float(origin["latitudeDeg"]))
        * earth_radius
    )
    up_offset = float(track_origin["altitudeM"]) - float(origin["altitudeM"])
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
    nearest = np.array([
        np.min(np.linalg.norm(reference[:, [0, 2]] - row[[0, 2]], axis=1))
        for row in ac
    ])
    rms = float(np.sqrt((nearest ** 2).mean()))
    # The 4x4 matrix maps (e, n, u) to (x, y, z); the horizontal
    # rotation is rows 0 and 2, columns 0 and 1.
    return ac, rms, matrix[[0, 2], :2]


def _reference_heights(reference_xyz, query_xyz):
    """Interpolate reference Y at each query's nearest X/Z path segment."""
    reference = np.asarray(reference_xyz, dtype=np.float64)
    query = np.asarray(query_xyz, dtype=np.float64)
    if reference.ndim != 2 or reference.shape[1] != 3 or len(reference) < 2:
        raise ValueError("Height reference needs at least two XYZ points")
    if not np.allclose(reference[0], reference[-1]):
        reference = np.vstack((reference, reference[0]))
    start = reference[:-1]
    delta = reference[1:] - start
    horizontal = delta[:, [0, 2]]
    length_sq = np.einsum("ij,ij->i", horizontal, horizontal)
    result = np.empty(len(query), dtype=np.float64)
    horizontal_distance = np.empty(len(query), dtype=np.float64)
    for index, point in enumerate(query):
        relative = point[[0, 2]] - start[:, [0, 2]]
        along = np.zeros(len(start), dtype=np.float64)
        valid = length_sq > 1e-12
        along[valid] = (
            np.einsum("ij,ij->i", relative[valid], horizontal[valid])
            / length_sq[valid]
        )
        along = np.clip(along, 0.0, 1.0)
        projected = start[:, [0, 2]] + horizontal * along[:, None]
        distance_sq = np.einsum(
            "ij,ij->i", point[[0, 2]] - projected, point[[0, 2]] - projected
        )
        segment = int(np.argmin(distance_sq))
        result[index] = start[segment, 1] + along[segment] * delta[segment, 1]
        horizontal_distance[index] = math.sqrt(float(distance_sq[segment]))
    return result, horizontal_distance


def align_replay_heights(mapped_xyz, reference_xyz, mode="track", offset_m=0.0):
    """Align telemetry heights to the simulator reference path.

    Track mode replaces GPS altitude with the nearest AC reference-segment
    height. GPS-offset mode preserves the GPS elevation profile but removes
    its median datum offset. GPS mode keeps the mapped GPS height unchanged.
    The offset is applied last for small car/body corrections.
    """
    mapped = np.asarray(mapped_xyz, dtype=np.float64)
    if mapped.ndim != 2 or mapped.shape[1] != 3:
        raise ValueError("Mapped replay positions must be an Nx3 array")
    if mode not in ("track", "gps-offset", "gps"):
        raise ValueError("Unknown height mode: {}".format(mode))
    reference_y, horizontal_distance = _reference_heights(reference_xyz, mapped)
    before = mapped[:, 1] - reference_y
    aligned = mapped.copy()
    datum_offset = float(np.median(before))
    if mode == "track":
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
    return aligned, diagnostics


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
                "currentLapTimeMs": int(round(index * RECORDING_INTERVAL_MS)),
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
    channel_overrides=None,
    gps_track_path=None,
    height_mode="track",
    height_offset_m=0.0,
    position_smoothing_s=0.75,
    wheel_steer_multiplier=1.0,
):
    """Convert one MoTeC lap using a native same-layout replay template."""
    points = extract_motec_points(
        ld_path,
        target_lap=lap,
        channel_overrides=channel_overrides or {},
    )
    replay = parse_acreplay(template_path, max_frames=0)
    if car_index < 0 or car_index >= len(replay["cars"]):
        raise ValueError("Car index {} out of range".format(car_index))
    car = replay["cars"][car_index]
    template_xyz = np.array([frame["positionM"] for frame in car["frames"]])

    if gps_track_path:
        with open(Path(gps_track_path).expanduser(), encoding="utf-8") as handle:
            track_ref = json.load(handle)
        validate_track_reference(replay, track_ref)
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
                    point["xM"] * rotation[0, 0] + point["yM"] * rotation[0, 1]
                    + translation[0],
                    point["zM"],
                    point["xM"] * rotation[1, 0] + point["yM"] * rotation[1, 1]
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
    )
    poses, frame_count = build_poses_from_xyz(
        points["points"],
        transformed,
        rotation,
        yaw_smoothing_s=position_smoothing_s,
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        if frame_count != car["numFrames"]:
            resampled = str(tmp_dir / "template_resampled.acreplay")
            resample(template_path, resampled, frame_count)
            template_path = resampled
        morph(
            template_path,
            output_path,
            car_index=car_index,
            poses=poses,
            wheel_steer_multiplier=wheel_steer_multiplier,
        )

    check = parse_acreplay(output_path, max_frames=2)
    return {
        "output": str(output_path),
        "fileSizeBytes": check["fileSizeBytes"],
        "frameCount": check["header"]["numFrames"],
        "carIndex": car_index,
        "driverName": check["cars"][car_index]["driverName"],
        "selectedLap": points["selectedLap"],
        "lapTimeS": points["lapTimeS"],
        "sourcePointCount": len(points["points"]),
        "sourceFrequencyHz": points["frequencyHz"],
        "sourceTrack": points["metadata"]["trackName"],
        "sourceCar": points["metadata"]["carName"],
        "wheelSteerMultiplier": float(wheel_steer_multiplier),
        "alignmentMethod": alignment_method,
        "horizontalAlignmentRmseM": float(rms),
        **smoothing_diagnostics,
        **height_diagnostics,
    }
