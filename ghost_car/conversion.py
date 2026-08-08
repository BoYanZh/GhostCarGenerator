"""Track-agnostic resampling and pose conversion for the ghost_car package."""

import bisect
import math
from typing import Any, Dict, List, Optional, Sequence


def _solve(matrix: List[List[float]], vector: List[float]) -> List[float]:
    size = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-15:
            raise ValueError("Singular polynomial fit")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * base
                for value, base in zip(augmented[row], augmented[column])
            ]
    return [augmented[row][-1] for row in range(size)]


def polynomial_smooth(
    values: Sequence[float],
    window: int,
    order: int,
) -> List[float]:
    """Local polynomial smoothing equivalent to a Savitzky-Golay value filter."""
    values = [float(value) for value in values]
    if window == 0:
        return values
    if window < 3 or window % 2 == 0:
        raise ValueError("Smoothing window must be zero or an odd integer >= 3")
    if order < 1 or order >= window:
        raise ValueError("Smoothing order must be >= 1 and smaller than the window")
    if len(values) <= order:
        return values
    half = window // 2
    result = []
    for center in range(len(values)):
        start = max(0, center - half)
        end = min(len(values), center + half + 1)
        if end - start <= order:
            start = max(0, min(start, len(values) - order - 1))
            end = min(len(values), start + order + 1)
        xs = [float(index - center) for index in range(start, end)]
        ys = values[start:end]
        normal = []
        rhs = []
        for row in range(order + 1):
            normal.append(
                [
                    sum(x ** (row + column) for x in xs)
                    for column in range(order + 1)
                ]
            )
            rhs.append(sum(y * x ** row for x, y in zip(xs, ys)))
        result.append(_solve(normal, rhs)[0])
    return result


def cumulative_distances(points: Sequence[Dict[str, Any]]) -> List[float]:
    if len(points) < 2:
        raise ValueError("At least two path points are required")
    distances = [0.0]
    for previous, current in zip(points, points[1:]):
        dx = float(current["xM"]) - float(previous["xM"])
        dy = float(current["yM"]) - float(previous["yM"])
        dz = float(current.get("zM", 0.0)) - float(previous.get("zM", 0.0))
        distances.append(distances[-1] + max(math.sqrt(dx * dx + dy * dy + dz * dz), 1e-9))
    return distances


def interpolate_point(
    points: Sequence[Dict[str, Any]],
    distances: Sequence[float],
    target: float,
) -> Dict[str, Any]:
    if target <= 0:
        return dict(points[0])
    if target >= distances[-1]:
        return dict(points[-1])
    left = max(0, bisect.bisect_right(distances, target) - 1)
    right = min(len(points) - 1, left + 1)
    span = distances[right] - distances[left]
    fraction = (target - distances[left]) / span if span else 0.0
    output = {}
    keys = set(points[left]) | set(points[right])
    for key in keys:
        first = points[left].get(key)
        second = points[right].get(key, first)
        if isinstance(first, (int, float)) and isinstance(second, (int, float)):
            output[key] = float(first) + fraction * (float(second) - float(first))
        else:
            output[key] = first
    return output


def _control_raw(value: Any, scale: str) -> int:
    number = float(value or 0.0)
    if scale == "percent":
        number /= 100.0
    elif scale == "raw":
        return max(0, min(255, int(round(number))))
    elif scale != "fraction":
        raise ValueError("Control scale must be fraction, percent, or raw")
    return max(0, min(255, int(round(number * 255.0))))


def _gear_for_point(
    point: Dict[str, Any],
    default_gear: int,
    thresholds_kph: Sequence[float],
) -> int:
    gear = point.get("gear")
    if gear is not None and float(gear) > 0:
        return max(0, min(255, int(round(float(gear)))))
    if thresholds_kph:
        speed_kph = float(point.get("speedMS", 0.0)) * 3.6
        return 1 + sum(speed_kph >= threshold for threshold in thresholds_kph)
    return max(0, min(255, int(default_gear)))


def _pose_from_tangent(
    points: Sequence[Dict[str, Any]],
    index: int,
) -> Dict[str, float]:
    previous = points[max(0, index - 1)]
    following = points[min(len(points) - 1, index + 1)]
    dx = float(following["xM"]) - float(previous["xM"])
    dy = float(following["yM"]) - float(previous["yM"])
    dz = float(following.get("zM", 0.0)) - float(previous.get("zM", 0.0))
    horizontal = math.hypot(dx, dy)
    return {
        "yaw": math.atan2(dy, dx) if horizontal else 0.0,
        "pitch": math.atan2(dz, horizontal) if horizontal or dz else 0.0,
    }


def build_canonical_blap(
    points: Sequence[Dict[str, Any]],
    template: Dict[str, Any],
    header_overrides: Optional[Dict[str, Any]] = None,
    sector_ends: Optional[Sequence[float]] = None,
    sector_bins: Optional[Sequence[int]] = None,
    yaw_source: str = "tangent",
    pitch_source: str = "tangent",
    roll_source: str = "input",
    smoothing_window: int = 35,
    smoothing_order: int = 3,
    min_time_step: float = 0.0,
    control_scale: str = "percent",
    default_gear: int = 1,
    gear_thresholds_kph: Sequence[float] = (),
    default_brake: float = 0.0,
    default_throttle: float = 0.0,
    system_raw: int = 0xFF,
    brake_light_threshold: int = 10,
    yaw_offset_deg: float = 0.0,
    pitch_offset_deg: float = 0.0,
    roll_offset_deg: float = 0.0,
) -> Dict[str, Any]:
    if yaw_source not in ("tangent", "input", "template"):
        raise ValueError("Yaw source must be tangent, input, or template")
    if pitch_source not in ("tangent", "input", "template"):
        raise ValueError("Pitch source must be tangent, input, or template")
    if roll_source not in ("zero", "input", "template"):
        raise ValueError("Roll source must be zero, input, or template")
    distances = cumulative_distances(points)
    raw_total = distances[-1]
    template_sectors = template.get("summary", {}).get("sectors", [])
    if not template_sectors:
        raise ValueError("BLAP template has no sector records")
    if sector_ends is None:
        sector_ends = [float(sector["endDistanceM"]) for sector in template_sectors]
    else:
        sector_ends = [float(number) for number in sector_ends]
    if sector_bins is None:
        sector_bins = [int(sector["numBins"]) for sector in template_sectors]
    else:
        sector_bins = [int(number) for number in sector_bins]
    if len(sector_ends) != len(template_sectors) or len(sector_bins) != len(template_sectors):
        raise ValueError("Sector overrides must match the template record count")
    if any(number <= 0 for number in sector_bins):
        raise ValueError("Every sector bin count must be positive")
    if any(current <= previous for previous, current in zip(sector_ends, sector_ends[1:])):
        raise ValueError("Sector end distances must be strictly increasing")

    track_start = float(template_sectors[0].get("startDistanceM", 0.0))
    track_length = sector_ends[-1] - track_start
    if track_length <= 0:
        raise ValueError("Template track length must be positive")
    template_samples = template.get("samples", [])
    sectors = []
    resampled_points = []
    sector_times = []
    previous_end = track_start
    previous_raw_end = 0.0
    for index, (end, bins) in enumerate(zip(sector_ends, sector_bins)):
        raw_end = raw_total * ((end - track_start) / track_length)
        raw_start = previous_raw_end
        start_time = float(interpolate_point(points, distances, raw_start).get("timeS", 0.0))
        end_time = float(interpolate_point(points, distances, raw_end).get("timeS", 0.0))
        times = []
        local_points = []
        for bin_index in range(bins):
            fraction = bin_index / max(1, bins - 1)
            raw_distance = raw_start + fraction * (raw_end - raw_start)
            point = interpolate_point(points, distances, raw_distance)
            local_points.append(point)
            times.append(float(point.get("timeS", 0.0)) - start_time)
        times = polynomial_smooth(times, smoothing_window, smoothing_order)
        if times:
            origin = times[0]
            times = [number - origin for number in times]
            for time_index in range(1, len(times)):
                times[time_index] = max(
                    times[time_index],
                    times[time_index - 1] + min_time_step,
                )
        resampled_points.extend(local_points)
        sector_times.extend(times)
        template_sector = template_sectors[index]
        sectors.append(
            {
                "sectorIndex": index + 1,
                "startDistanceM": previous_end,
                "endDistanceM": end,
                "sectorLengthM": end - previous_end,
                "numBins": bins,
                "sampleSpacingM": (end - previous_end) / max(1, bins - 1),
                "unknownFloat1": template_sector.get("unknownFloat1", 0.0),
                "unknownFloat2": template_sector.get("unknownFloat2", 0.0),
                "sectorBestTimeS": end_time - start_time,
                "recordFlags": template_sector.get("recordFlags", 0),
            }
        )
        previous_end = end
        previous_raw_end = raw_end

    samples = []
    sector_index = 0
    sector_bin = 0
    for index, point in enumerate(resampled_points):
        while (
            sector_index + 1 < len(sector_bins)
            and sector_bin >= sector_bins[sector_index]
        ):
            sector_index += 1
            sector_bin = 0
        tangent = _pose_from_tangent(resampled_points, index)
        reference = template_samples[index] if index < len(template_samples) else {}
        if yaw_source == "template":
            yaw = float(reference.get("yawRad", 0.0))
        elif yaw_source == "input":
            yaw = float(point.get("headingRad", tangent["yaw"]))
        else:
            yaw = tangent["yaw"]
        if pitch_source == "template":
            pitch = float(reference.get("pitchRad", 0.0))
        elif pitch_source == "input":
            pitch = float(point.get("pitchRad", tangent["pitch"]))
        else:
            pitch = tangent["pitch"]
        if roll_source == "template":
            roll = float(reference.get("rollRad", 0.0))
        elif roll_source == "input":
            roll = float(point.get("rollRad", 0.0))
        else:
            roll = 0.0
        time_s = sector_times[index]
        reference_time = float(reference.get("timeInSectorS", time_s))
        brake = _control_raw(point.get("brake", default_brake), control_scale)
        throttle = _control_raw(point.get("throttle", default_throttle), control_scale)
        gear = _gear_for_point(point, default_gear, gear_thresholds_kph)
        flags = (
            ((gear & 0xFF) << 24)
            | ((int(system_raw) & 0xFF) << 16)
            | ((brake & 0xFF) << 8)
            | (throttle & 0xFF)
        )
        yaw += math.radians(yaw_offset_deg)
        pitch += math.radians(pitch_offset_deg)
        roll += math.radians(roll_offset_deg)
        samples.append(
            {
                "globalBinIndex": index,
                "sectorIndex": sector_index + 1,
                "timeInSectorS": time_s,
                "deltaS": time_s - reference_time,
                "yawRad": yaw,
                "pitchRad": pitch,
                "rollRad": roll,
                "reserved": 0.0,
                "flags": flags,
                "gear": gear,
                "systemRaw": int(system_raw) & 0xFF,
                "brakeRaw": brake,
                "throttleRaw": throttle,
                "brakeLightOn": brake > brake_light_threshold,
            }
        )
        sector_bin += 1

    header = dict(template.get("header", {}))
    if header_overrides:
        header.update(
            {key: value for key, value in header_overrides.items() if value is not None}
        )
    first_time = float(points[0].get("timeS", 0.0))
    last_time = float(points[-1].get("timeS", 0.0))
    return {
        "header": header,
        "summary": {
            "bestLapS": last_time - first_time,
            "tableTypeCount": template.get("summary", {}).get("tableTypeCount", 0),
            "sectorCount": len(sectors),
            "totalTrackLengthM": sector_ends[-1],
            "totalBins": len(samples),
            "sectors": sectors,
        },
        "samples": samples,
        "binary": dict(template.get("binary", {})),
    }
