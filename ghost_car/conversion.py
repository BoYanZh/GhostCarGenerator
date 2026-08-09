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
    clutch_raw: int = 0xFF,
    brake_light_threshold: int = 10,
    yaw_offset_deg: float = 0.0,
    pitch_offset_deg: float = 0.0,
    roll_offset_deg: float = 0.0,
    distance_map: Optional[Dict[str, Sequence[float]]] = None,
    lateral_offset_source: str = "auto",
    apply_alignment_rotation: bool = True,
    sector_boundary_source: str = "generated",
) -> Dict[str, Any]:
    if yaw_source not in ("tangent", "input", "template"):
        raise ValueError("Yaw source must be tangent, input, or template")
    if pitch_source not in ("tangent", "input", "template"):
        raise ValueError("Pitch source must be tangent, input, or template")
    if roll_source not in ("zero", "input", "template"):
        raise ValueError("Roll source must be zero, input, or template")
    if lateral_offset_source not in ("auto", "alignment", "template", "zero"):
        raise ValueError(
            "Lateral-offset source must be auto, alignment, template, or zero"
        )
    if sector_boundary_source not in ("generated", "template", "zero"):
        raise ValueError("Sector-boundary source must be generated, template, or zero")
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
    mapped_targets = None
    mapped_sources = None
    mapped_lateral_offsets = None
    alignment_rotation_rad = 0.0
    if distance_map is not None:
        mapped_targets = [float(value) for value in distance_map["targetDistanceM"]]
        mapped_sources = [float(value) for value in distance_map["sourceDistanceM"]]
        if len(mapped_targets) != len(mapped_sources) or len(mapped_targets) < 2:
            raise ValueError("Distance map arrays must have the same length >= 2")
        if any(
            current <= previous
            for previous, current in zip(mapped_targets, mapped_targets[1:])
        ):
            raise ValueError("Distance-map target distances must be strictly increasing")
        if any(
            current <= previous
            for previous, current in zip(mapped_sources, mapped_sources[1:])
        ):
            raise ValueError("Distance-map source distances must be strictly increasing")
        tolerance = max(1e-6, track_length * 1e-6)
        if abs(mapped_targets[0]) > tolerance or abs(mapped_targets[-1] - track_length) > tolerance:
            raise ValueError("Distance-map target endpoints must span the template track")
        if abs(mapped_sources[0]) > tolerance or abs(mapped_sources[-1] - raw_total) > tolerance:
            raise ValueError("Distance-map source endpoints must span the input lap")
        if "lateralOffsetM" in distance_map:
            mapped_lateral_offsets = [
                float(value) for value in distance_map["lateralOffsetM"]
            ]
            if len(mapped_lateral_offsets) != len(mapped_targets):
                raise ValueError(
                    "Distance-map lateral offsets must match its distance arrays"
                )
        if apply_alignment_rotation:
            alignment_rotation_rad = math.radians(
                float(distance_map.get("headingRotationDeg", 0.0))
            )

    def source_distance(target_distance: float) -> float:
        relative_target = target_distance - track_start
        if mapped_targets is None or mapped_sources is None:
            return raw_total * (relative_target / track_length)
        if relative_target <= mapped_targets[0]:
            return mapped_sources[0]
        if relative_target >= mapped_targets[-1]:
            return mapped_sources[-1]
        left = max(0, bisect.bisect_right(mapped_targets, relative_target) - 1)
        right = min(len(mapped_targets) - 1, left + 1)
        span = mapped_targets[right] - mapped_targets[left]
        fraction = (
            (relative_target - mapped_targets[left]) / span if span else 0.0
        )
        return mapped_sources[left] + fraction * (
            mapped_sources[right] - mapped_sources[left]
        )

    template_samples = template.get("samples", [])
    template_distances = []
    template_lateral_offsets = []
    template_sample_index = 0
    for sector in template_sectors:
        start = float(sector["startDistanceM"])
        end = float(sector["endDistanceM"])
        bins = int(sector["numBins"])
        for bin_index in range(bins):
            fraction = bin_index / max(1, bins - 1)
            distance = start + fraction * (end - start)
            sample = (
                template_samples[template_sample_index]
                if template_sample_index < len(template_samples)
                else {}
            )
            offset = float(
                sample.get("lateralOffsetM", sample.get("deltaS", 0.0))
            )
            if template_distances and abs(distance - template_distances[-1]) <= 1e-6:
                template_lateral_offsets[-1] = 0.5 * (
                    template_lateral_offsets[-1] + offset
                )
            else:
                template_distances.append(distance)
                template_lateral_offsets.append(offset)
            template_sample_index += 1

    def interpolate_series(
        grid: Sequence[float],
        values: Sequence[float],
        target: float,
    ) -> float:
        if target <= grid[0]:
            return float(values[0])
        if target >= grid[-1]:
            return float(values[-1])
        left = max(0, bisect.bisect_right(grid, target) - 1)
        right = min(len(grid) - 1, left + 1)
        span = grid[right] - grid[left]
        fraction = (target - grid[left]) / span if span else 0.0
        return float(values[left]) + fraction * (
            float(values[right]) - float(values[left])
        )

    resolved_lateral_source = lateral_offset_source
    if resolved_lateral_source == "auto":
        resolved_lateral_source = (
            "alignment" if mapped_lateral_offsets is not None else "template"
        )
    if resolved_lateral_source == "alignment" and mapped_lateral_offsets is None:
        raise ValueError(
            "Alignment lateral offsets require a fitted target-track reference"
        )

    def lateral_offset(target_distance: float) -> float:
        if resolved_lateral_source == "zero":
            return 0.0
        if resolved_lateral_source == "alignment":
            return interpolate_series(
                mapped_targets,
                mapped_lateral_offsets,
                target_distance - track_start,
            )
        if not template_distances:
            raise ValueError("Template has no samples for lateral-offset passthrough")
        return interpolate_series(
            template_distances,
            template_lateral_offsets,
            target_distance,
        )

    sectors = []
    resampled_points = []
    sample_target_distances = []
    sector_times = []
    previous_end = track_start
    previous_raw_end = source_distance(track_start)
    for index, (end, bins) in enumerate(zip(sector_ends, sector_bins)):
        raw_end = source_distance(end)
        raw_start = previous_raw_end
        start_time = float(interpolate_point(points, distances, raw_start).get("timeS", 0.0))
        end_time = float(interpolate_point(points, distances, raw_end).get("timeS", 0.0))
        times = []
        local_points = []
        for bin_index in range(bins):
            fraction = bin_index / max(1, bins - 1)
            target_distance = previous_end + fraction * (end - previous_end)
            raw_distance = source_distance(target_distance)
            point = interpolate_point(points, distances, raw_distance)
            local_points.append(point)
            sample_target_distances.append(target_distance)
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
                "startBoundaryVerticalOffsetM": template_sector.get(
                    "startBoundaryVerticalOffsetM",
                    template_sector.get("unknownFloat1", 0.0),
                ),
                "endBoundaryVerticalOffsetM": template_sector.get(
                    "endBoundaryVerticalOffsetM",
                    template_sector.get("unknownFloat2", 0.0),
                ),
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
        if yaw_source != "template":
            yaw += alignment_rotation_rad
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
        brake = _control_raw(point.get("brake", default_brake), control_scale)
        throttle = _control_raw(point.get("throttle", default_throttle), control_scale)
        gear = _gear_for_point(point, default_gear, gear_thresholds_kph)
        flags = (
            ((gear & 0xFF) << 24)
            | ((int(clutch_raw) & 0xFF) << 16)
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
                "lateralOffsetM": lateral_offset(sample_target_distances[index]),
                "yawRad": yaw,
                "pitchRad": pitch,
                "rollRad": roll,
                "reserved": 0.0,
                "flags": flags,
                "gear": gear,
                "clutchRaw": int(clutch_raw) & 0xFF,
                "brakeRaw": brake,
                "throttleRaw": throttle,
                "brakeLightOn": brake > brake_light_threshold,
            }
        )
        sector_bin += 1

    boundary_cross_slope = None
    if sector_boundary_source == "zero":
        for sector in sectors:
            sector["startBoundaryVerticalOffsetM"] = 0.0
            sector["endBoundaryVerticalOffsetM"] = 0.0
    elif sector_boundary_source == "generated":
        template_start_lateral = template_lateral_offsets[0]
        template_end_lateral = template_lateral_offsets[-1]
        template_start_vertical = float(
            template_sectors[0].get(
                "startBoundaryVerticalOffsetM",
                template_sectors[0].get("unknownFloat1", 0.0),
            )
        )
        template_end_vertical = float(
            template_sectors[-1].get(
                "endBoundaryVerticalOffsetM",
                template_sectors[-1].get("unknownFloat2", 0.0),
            )
        )
        denominator = (
            template_start_lateral * template_start_lateral
            + template_end_lateral * template_end_lateral
        )
        if denominator <= 1e-12:
            raise ValueError(
                "Target profile cannot identify its start/finish cross-slope; "
                "use --sector-boundary-source template or zero"
            )
        boundary_cross_slope = (
            template_start_lateral * template_start_vertical
            + template_end_lateral * template_end_vertical
        ) / denominator
        sectors[0]["startBoundaryVerticalOffsetM"] = (
            boundary_cross_slope * float(samples[0]["lateralOffsetM"])
        )
        sectors[-1]["endBoundaryVerticalOffsetM"] = (
            boundary_cross_slope * float(samples[-1]["lateralOffsetM"])
        )

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
            "tableVersionCandidate": template.get("summary", {}).get(
                "tableVersionCandidate",
                template.get("summary", {}).get("tableTypeCount", 0),
            ),
            "sectorCount": len(sectors),
            "totalTrackLengthM": sector_ends[-1],
            "totalBins": len(samples),
            "sectors": sectors,
            "boundaryCrossSlope": boundary_cross_slope,
            "boundaryCrossSlopeDeg": (
                math.degrees(math.atan(boundary_cross_slope))
                if boundary_cross_slope is not None
                else None
            ),
        },
        "samples": samples,
        "binary": dict(template.get("binary", {})),
    }
