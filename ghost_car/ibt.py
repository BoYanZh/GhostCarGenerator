"""Native iRacing IBT reference parsing and GPS-based path alignment."""

__all__ = [
    "load_ibt_reference",
    "build_blap_track_reference",
    "average_track_references",
    "build_matched_blap_ibt_track_reference",
    "fit_ibt_distance_map",
    "combine_alignment_maps",
]

import math
import mmap
import numpy as np
import struct
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple


_IBT_TYPES = {
    0: "i1",
    1: "i1",
    2: "<i4",
    3: "<u4",
    4: "<f4",
    5: "<f8",
}


def _yaml_value(text: str, key: str) -> str:
    prefix = key + ":"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped.split(":", 1)[1].strip()
    return ""


def load_ibt_reference(
    ibt_path: str,
    target_lap: Optional[int] = None,
    lap_selection: str = "fastest",
    min_lap_seconds: float = 60.0,
    max_lap_seconds: float = 300.0,
    earth_radius_m: float = 6371008.8,
    target_lap_time_s: Optional[float] = None,
    max_lap_time_difference_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Load one complete IBT lap as a local east/north GPS path."""
    if lap_selection not in ("fastest", "first"):
        raise ValueError("IBT lap selection must be fastest or first")
    if target_lap is not None and target_lap <= 0:
        raise ValueError("IBT lap number must be positive")
    if min_lap_seconds < 0 or max_lap_seconds <= min_lap_seconds:
        raise ValueError("IBT lap duration bounds are invalid")
    if earth_radius_m <= 0:
        raise ValueError("Earth radius must be positive")
    if target_lap_time_s is not None and target_lap_time_s <= 0:
        raise ValueError("Target IBT lap time must be positive")
    if (
        max_lap_time_difference_s is not None
        and max_lap_time_difference_s < 0
    ):
        raise ValueError("Maximum IBT lap-time difference cannot be negative")

    path = Path(ibt_path).expanduser()
    with path.open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            if len(raw) < 112:
                raise ValueError("IBT file is too small")
            (
                version,
                _status,
                tick_rate,
                _session_update,
                session_length,
                session_offset,
                variable_count,
                variable_offset,
                _buffer_count,
                buffer_length,
                _padding0,
                _padding1,
            ) = struct.unpack_from("<12i", raw, 0)
            _tick_count, buffer_offset = struct.unpack_from("<2i", raw, 48)
            if tick_rate <= 0 or buffer_length <= 0:
                raise ValueError("IBT tick rate and buffer length must be positive")
            if buffer_offset <= 0 or buffer_offset >= len(raw):
                raise ValueError("IBT data buffer offset is invalid")
            sample_count = (len(raw) - buffer_offset) // buffer_length
            if sample_count < 2:
                raise ValueError("IBT file contains fewer than two samples")

            variables = {}
            for index in range(variable_count):
                base = variable_offset + index * 144
                if base + 144 > len(raw):
                    raise ValueError("IBT variable header table is truncated")
                value_type, value_offset, value_count = struct.unpack_from(
                    "<3i", raw, base
                )
                name = raw[base + 16 : base + 48].split(b"\0")[0].decode("latin-1")
                unit = raw[base + 112 : base + 144].split(b"\0")[0].decode(
                    "latin-1"
                )
                dtype = _IBT_TYPES.get(value_type)
                if dtype is not None:
                    variables[name] = (np.dtype(dtype), value_offset, value_count, unit)

            def extract(name: str):
                if name not in variables:
                    raise ValueError("IBT variable {!r} is missing".format(name))
                dtype, offset, count, _unit = variables[name]
                if count != 1:
                    raise ValueError("IBT variable {!r} is not scalar".format(name))
                return np.ndarray(
                    (sample_count,),
                    dtype=dtype,
                    buffer=raw,
                    offset=buffer_offset + offset,
                    strides=(buffer_length,),
                ).astype(np.float64)

            session_time = extract("SessionTime")
            lap_fraction = extract("LapDistPct")
            latitude = extract("Lat")
            longitude = extract("Lon")
            altitude = extract("Alt")
            session_text = raw[
                session_offset : session_offset + session_length
            ].decode("latin-1", errors="ignore")

    wraps = np.where(
        (lap_fraction[:-1] > 0.95) & (lap_fraction[1:] < 0.05)
    )[0] + 1
    candidates = []
    for lap_number, (start, end) in enumerate(zip(wraps, wraps[1:]), start=1):
        duration = float(session_time[end] - session_time[start])
        lat = latitude[start:end]
        lon = longitude[start:end]
        valid_coordinates = (
            np.all(np.isfinite(lat))
            and np.all(np.isfinite(lon))
            and np.all(np.abs(lat) > 1.0)
            and np.all(np.abs(lon) > 1.0)
            and np.all(np.abs(lat) <= 90.0)
            and np.all(np.abs(lon) <= 180.0)
        )
        if (
            min_lap_seconds <= duration <= max_lap_seconds
            and valid_coordinates
            and end - start >= 2
        ):
            candidates.append((duration, lap_number, start, end))
    if not candidates:
        raise ValueError("IBT file contains no complete valid GPS lap")
    if target_lap is not None:
        selected = next(
            (item for item in candidates if item[1] == target_lap),
            None,
        )
        if selected is None:
            raise ValueError("Requested IBT lap {} is not valid".format(target_lap))
    elif target_lap_time_s is not None:
        selected = min(
            candidates,
            key=lambda item: abs(item[0] - target_lap_time_s),
        )
    elif lap_selection == "fastest":
        selected = min(candidates, key=lambda item: item[0])
    else:
        selected = candidates[0]
    duration, lap_number, start, end = selected
    if (
        target_lap_time_s is not None
        and max_lap_time_difference_s is not None
    ):
        difference = abs(duration - target_lap_time_s)
        if difference > max_lap_time_difference_s:
            raise ValueError(
                "Selected IBT lap differs from BLAP by {:.3f}s; limit is {:.3f}s".format(
                    difference,
                    max_lap_time_difference_s,
                )
            )

    fraction = lap_fraction[start:end].copy()
    lat = latitude[start:end].copy()
    lon = longitude[start:end].copy()
    alt = altitude[start:end].copy()
    order = np.argsort(fraction)
    fraction = fraction[order]
    lat = lat[order]
    lon = lon[order]
    alt = alt[order]
    keep = np.concatenate(([True], np.diff(fraction) > 1e-7))
    fraction = fraction[keep]
    lat = lat[keep]
    lon = lon[keep]
    alt = alt[keep]
    if len(fraction) < 2:
        raise ValueError("Selected IBT lap has insufficient unique distance samples")

    latitude_origin = float(np.mean(lat))
    longitude_origin = float(np.mean(lon))
    altitude_origin = float(np.mean(alt))
    east = (
        np.radians(lon - longitude_origin)
        * earth_radius_m
        * math.cos(math.radians(latitude_origin))
    )
    north = np.radians(lat - latitude_origin) * earth_radius_m
    up = alt - altitude_origin
    return {
        "lapFraction": fraction.tolist(),
        "eastM": east.tolist(),
        "northM": north.tolist(),
        "upM": up.tolist(),
        "selectedLap": lap_number,
        "lapTimeS": duration,
        "tickRateHz": tick_rate,
        "version": version,
        "origin": {
            "latitudeDeg": latitude_origin,
            "longitudeDeg": longitude_origin,
            "altitudeM": altitude_origin,
        },
        "geodetic": {
            "latitudeDeg": lat.tolist(),
            "longitudeDeg": lon.tolist(),
            "altitudeM": alt.tolist(),
        },
        "metadata": {
            "trackName": _yaml_value(session_text, "TrackDisplayName"),
            "carName": _yaml_value(session_text, "CarScreenName"),
            "driverName": _yaml_value(session_text, "UserName"),
        },
    }


def _cumulative_distances(points: Sequence[Dict[str, Any]]):
    xyz = np.asarray(
        [
            [
                float(point["xM"]),
                float(point["yM"]),
                float(point.get("zM", 0.0)),
            ]
            for point in points
        ],
        dtype=np.float64,
    )
    steps = np.linalg.norm(np.diff(xyz, axis=0), axis=1)
    steps = np.maximum(steps, 1e-9)
    return xyz, np.concatenate(([0.0], np.cumsum(steps)))


def _fit_similarity(source, target, allow_scale: bool):
    source_mean = np.mean(source, axis=0)
    target_mean = np.mean(target, axis=0)
    centered_source = source - source_mean
    centered_target = target - target_mean
    left, singular, right = np.linalg.svd(centered_source.T @ centered_target)
    rotation = left @ right
    scale_numerator = float(np.sum(singular))
    if np.linalg.det(rotation) < 0:
        left[:, -1] *= -1
        rotation = left @ right
        scale_numerator -= 2.0 * float(singular[-1])
    denominator = float(np.sum(centered_source * centered_source))
    if denominator <= 0:
        raise ValueError("Source path has no spatial extent")
    scale = scale_numerator / denominator if allow_scale else 1.0
    translation = target_mean - scale * source_mean @ rotation
    return scale, rotation, translation


def _project_polyline(queries, path, chunk_size: int = 128):
    starts = path[:-1]
    vectors = path[1:] - starts
    lengths_squared = np.sum(vectors * vectors, axis=1)
    lengths_squared = np.maximum(lengths_squared, 1e-12)
    output_points = np.empty_like(queries)
    output_distances = np.empty(len(queries), dtype=np.float64)
    output_segments = np.empty(len(queries), dtype=np.int64)
    output_fractions = np.empty(len(queries), dtype=np.float64)
    for start in range(0, len(queries), chunk_size):
        end = min(len(queries), start + chunk_size)
        difference = queries[start:end, None, :] - starts[None, :, :]
        fractions = np.clip(
            np.sum(difference * vectors[None, :, :], axis=2)
            / lengths_squared[None, :],
            0.0,
            1.0,
        )
        projections = starts[None, :, :] + fractions[:, :, None] * vectors[None, :, :]
        squared = np.sum(
            (queries[start:end, None, :] - projections) ** 2,
            axis=2,
        )
        local = np.argmin(squared, axis=1)
        rows = np.arange(end - start)
        output_points[start:end] = projections[rows, local]
        output_distances[start:end] = np.sqrt(squared[rows, local])
        output_segments[start:end] = local
        output_fractions[start:end] = fractions[rows, local]
    return output_points, output_distances, output_segments, output_fractions


def _moving_average(values, window: int):
    if window <= 1:
        return values.copy()
    if window % 2 == 0:
        window += 1
    half = window // 2
    padded = np.pad(values, (half, half), mode="edge")
    return np.convolve(padded, np.ones(window) / window, mode="valid")


def _isotonic(values, endpoint_weight: float):
    blocks = []
    for index, value in enumerate(values):
        weight = endpoint_weight if index in (0, len(values) - 1) else 1.0
        blocks.append([index, index, float(weight), float(value) * float(weight)])
        while len(blocks) >= 2:
            previous = blocks[-2][3] / blocks[-2][2]
            current = blocks[-1][3] / blocks[-1][2]
            if previous <= current:
                break
            right = blocks.pop()
            left = blocks.pop()
            blocks.append(
                [left[0], right[1], left[2] + right[2], left[3] + right[3]]
            )
    output = np.empty(len(values), dtype=np.float64)
    for start, end, weight, weighted_sum in blocks:
        output[start : end + 1] = weighted_sum / weight
    return output


def build_blap_track_reference(
    template: Dict[str, Any],
    heading_smoothing_distance_m: float = 8.0,
    close_loop: bool = True,
) -> Dict[str, Any]:
    """Infer the iRacing reference spline represented by a BLAP/OLAP.

    The second sample float is a signed lateral offset.  For small offsets the
    observed relationship is yaw = spline_yaw + atan(d(offset) / ds).  Removing
    that term from an official lap gives a track-relative spline that can be
    used to project an external path into BLAP coordinates.
    """
    if heading_smoothing_distance_m < 0:
        raise ValueError("Reference-heading smoothing distance cannot be negative")
    sectors = template.get("summary", {}).get("sectors", [])
    samples = template.get("samples", [])
    expected = sum(int(sector["numBins"]) for sector in sectors)
    if not sectors or expected != len(samples):
        raise ValueError("Template sectors and samples do not form a complete lap")

    raw_distance = []
    raw_offset = []
    raw_yaw = []
    sample_index = 0
    for sector in sectors:
        start = float(sector["startDistanceM"])
        end = float(sector["endDistanceM"])
        bins = int(sector["numBins"])
        for bin_index in range(bins):
            fraction = bin_index / max(1, bins - 1)
            raw_distance.append(start + fraction * (end - start))
            sample = samples[sample_index]
            raw_offset.append(
                float(
                    sample.get(
                        "lateralOffsetM",
                        sample.get("deltaS", 0.0),
                    )
                )
            )
            raw_yaw.append(float(sample["yawRad"]))
            sample_index += 1

    distance = []
    offset = []
    yaw = []
    unwrapped_yaw = np.unwrap(np.asarray(raw_yaw, dtype=np.float64))
    index = 0
    while index < len(raw_distance):
        end = index + 1
        while end < len(raw_distance) and abs(raw_distance[end] - raw_distance[index]) <= 1e-6:
            end += 1
        distance.append(float(np.mean(raw_distance[index:end])))
        offset.append(float(np.mean(raw_offset[index:end])))
        yaw.append(float(np.mean(unwrapped_yaw[index:end])))
        index = end
    distance = np.asarray(distance, dtype=np.float64)
    offset = np.asarray(offset, dtype=np.float64)
    yaw = np.asarray(yaw, dtype=np.float64)
    if len(distance) < 3 or np.any(np.diff(distance) <= 0):
        raise ValueError("Template distance grid is not strictly increasing")

    lateral_gradient = np.gradient(offset, distance, edge_order=2)
    spline_heading = yaw - np.arctan(lateral_gradient)
    median_spacing = float(np.median(np.diff(distance)))
    smoothing_window = max(
        1,
        int(round(heading_smoothing_distance_m / max(median_spacing, 1e-12))),
    )
    spline_heading = _moving_average(spline_heading, smoothing_window)
    step = np.diff(distance)
    midpoint_heading = 0.5 * (spline_heading[:-1] + spline_heading[1:])
    x = np.concatenate(([0.0], np.cumsum(step * np.cos(midpoint_heading))))
    y = np.concatenate(([0.0], np.cumsum(step * np.sin(midpoint_heading))))
    if close_loop:
        fraction = (distance - distance[0]) / (distance[-1] - distance[0])
        x = x - fraction * x[-1]
        y = y - fraction * y[-1]
    track_length = float(distance[-1] - distance[0])
    return {
        "lapFraction": ((distance - distance[0]) / track_length).tolist(),
        "eastM": x.tolist(),
        "northM": y.tolist(),
        "upM": [0.0] * len(distance),
        "referenceKind": "blap-inferred-spline",
        "trackLengthM": track_length,
        "metadata": {
            "trackName": template.get("header", {}).get("trackName", ""),
            "carName": template.get("header", {}).get("carShortName", ""),
        },
    }


def average_track_references(
    references: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Average compatible inferred splines to reduce per-lap yaw/slip bias."""
    if not references:
        raise ValueError("At least one track reference is required")
    if len(references) == 1:
        return dict(references[0])
    sample_count = max(len(reference["lapFraction"]) for reference in references)
    fraction = np.linspace(0.0, 1.0, sample_count)
    east = []
    north = []
    for reference in references:
        source_fraction = np.asarray(reference["lapFraction"], dtype=np.float64)
        if (
            len(source_fraction) < 2
            or abs(float(source_fraction[0])) > 1e-9
            or abs(float(source_fraction[-1]) - 1.0) > 1e-9
        ):
            raise ValueError("Track reference does not span a complete lap")
        east.append(
            np.interp(fraction, source_fraction, reference["eastM"])
        )
        north.append(
            np.interp(fraction, source_fraction, reference["northM"])
        )
    metadata = dict(references[0].get("metadata", {}))
    metadata["referenceCount"] = len(references)
    return {
        "lapFraction": fraction.tolist(),
        "eastM": np.mean(east, axis=0).tolist(),
        "northM": np.mean(north, axis=0).tolist(),
        "upM": [0.0] * sample_count,
        "referenceKind": "blap-inferred-spline-ensemble",
        "trackLengthM": references[0].get("trackLengthM"),
        "metadata": metadata,
    }


def _periodic_interp(target, source_fraction, source_values):
    fraction = np.mod(
        np.asarray(source_fraction, dtype=np.float64),
        1.0,
    )
    values = np.asarray(source_values, dtype=np.float64)
    if len(fraction) != len(values) or len(fraction) < 2:
        raise ValueError("Periodic interpolation requires matching series")
    order = np.argsort(fraction)
    fraction = fraction[order]
    values = values[order]
    unique_fraction = []
    unique_values = []
    index = 0
    while index < len(fraction):
        end = index + 1
        while (
            end < len(fraction)
            and abs(float(fraction[end] - fraction[index])) <= 1e-10
        ):
            end += 1
        unique_fraction.append(float(np.mean(fraction[index:end])))
        unique_values.append(float(np.mean(values[index:end])))
        index = end
    fraction = np.asarray(unique_fraction, dtype=np.float64)
    values = np.asarray(unique_values, dtype=np.float64)
    if len(fraction) < 2:
        raise ValueError("Periodic interpolation has fewer than two unique points")
    extended_fraction = np.concatenate(
        (fraction[-1:] - 1.0, fraction, fraction[:1] + 1.0)
    )
    extended_values = np.concatenate((values[-1:], values, values[:1]))
    return np.interp(target, extended_fraction, extended_values)


def _circular_moving_average(values, window: int):
    array = np.asarray(values, dtype=np.float64)
    if window <= 1:
        return array.copy()
    if window % 2 == 0:
        window += 1
    output = np.zeros_like(array)
    half = window // 2
    for shift in range(-half, half + 1):
        output += np.roll(array, shift, axis=0)
    return output / window


def _geodetic_to_enu(latitude, longitude, altitude, origin):
    semi_major = 6378137.0
    flattening = 1.0 / 298.257223563
    eccentricity_squared = flattening * (2.0 - flattening)

    def to_ecef(lat_deg, lon_deg, alt_m):
        latitude_rad = np.radians(lat_deg)
        longitude_rad = np.radians(lon_deg)
        sin_latitude = np.sin(latitude_rad)
        cos_latitude = np.cos(latitude_rad)
        radius = semi_major / np.sqrt(
            1.0 - eccentricity_squared * sin_latitude * sin_latitude
        )
        return np.column_stack(
            (
                (radius + alt_m) * cos_latitude * np.cos(longitude_rad),
                (radius + alt_m) * cos_latitude * np.sin(longitude_rad),
                (
                    radius * (1.0 - eccentricity_squared) + alt_m
                ) * sin_latitude,
            )
        )

    latitude = np.asarray(latitude, dtype=np.float64)
    longitude = np.asarray(longitude, dtype=np.float64)
    altitude = np.asarray(altitude, dtype=np.float64)
    origin_latitude = float(origin["latitudeDeg"])
    origin_longitude = float(origin["longitudeDeg"])
    origin_altitude = float(origin["altitudeM"])
    points = to_ecef(latitude, longitude, altitude)
    origin_ecef = to_ecef(
        np.asarray([origin_latitude]),
        np.asarray([origin_longitude]),
        np.asarray([origin_altitude]),
    )[0]
    delta = points - origin_ecef
    latitude_rad = math.radians(origin_latitude)
    longitude_rad = math.radians(origin_longitude)
    sin_latitude = math.sin(latitude_rad)
    cos_latitude = math.cos(latitude_rad)
    sin_longitude = math.sin(longitude_rad)
    cos_longitude = math.cos(longitude_rad)
    east = -sin_longitude * delta[:, 0] + cos_longitude * delta[:, 1]
    north = (
        -sin_latitude * cos_longitude * delta[:, 0]
        - sin_latitude * sin_longitude * delta[:, 1]
        + cos_latitude * delta[:, 2]
    )
    up = (
        cos_latitude * cos_longitude * delta[:, 0]
        + cos_latitude * sin_longitude * delta[:, 1]
        + sin_latitude * delta[:, 2]
    )
    return np.column_stack((east, north, up))


def _blap_lateral_series(template: Dict[str, Any]):
    sectors = template.get("summary", {}).get("sectors", [])
    samples = template.get("samples", [])
    expected = sum(int(sector["numBins"]) for sector in sectors)
    if not sectors or expected != len(samples):
        raise ValueError("Matched BLAP sectors and samples do not form a complete lap")
    track_start = float(sectors[0].get("startDistanceM", 0.0))
    track_end = float(sectors[-1]["endDistanceM"])
    track_length = track_end - track_start
    if track_length <= 0:
        raise ValueError("Matched BLAP track length must be positive")
    distance = []
    offset = []
    sample_index = 0
    for sector in sectors:
        start = float(sector["startDistanceM"])
        end = float(sector["endDistanceM"])
        bins = int(sector["numBins"])
        for bin_index in range(bins):
            fraction = bin_index / max(1, bins - 1)
            distance.append(start + fraction * (end - start))
            sample = samples[sample_index]
            offset.append(
                float(
                    sample.get(
                        "lateralOffsetM",
                        sample.get("deltaS", 0.0),
                    )
                )
            )
            sample_index += 1
    distance = np.asarray(distance, dtype=np.float64)
    offset = np.asarray(offset, dtype=np.float64)
    unique_distance = []
    unique_offset = []
    index = 0
    while index < len(distance):
        end = index + 1
        while (
            end < len(distance)
            and abs(float(distance[end] - distance[index])) <= 1e-6
        ):
            end += 1
        unique_distance.append(float(np.mean(distance[index:end])))
        unique_offset.append(float(np.mean(offset[index:end])))
        index = end
    return (
        (np.asarray(unique_distance, dtype=np.float64) - track_start)
        / track_length,
        np.asarray(unique_offset, dtype=np.float64),
        track_length,
    )


def build_matched_blap_ibt_track_reference(
    pairs: Sequence[Dict[str, Any]],
    smoothing_distance_m: float = 8.0,
    min_lateral_separation_m: float = 1.0,
    min_usable_fraction: float = 0.25,
    max_track_length_difference_m: float = 0.01,
    max_iterations: int = 5,
) -> Dict[str, Any]:
    """Recover a shared spline from matched BLAP offsets and IBT GPS paths."""
    if len(pairs) < 2:
        raise ValueError("Matched reconstruction requires at least two BLAP/IBT pairs")
    if smoothing_distance_m < 0 or min_lateral_separation_m <= 0:
        raise ValueError("Matched reconstruction distances are invalid")
    if not 0.0 < min_usable_fraction <= 1.0:
        raise ValueError("Matched usable fraction must be in (0, 1]")
    if max_track_length_difference_m < 0 or max_iterations <= 0:
        raise ValueError("Matched reconstruction limits are invalid")

    all_latitude = []
    all_longitude = []
    all_altitude = []
    lateral_series = []
    track_lengths = []
    sample_count = 0
    for pair in pairs:
        fraction, offset, track_length = _blap_lateral_series(pair["template"])
        lateral_series.append((fraction, offset))
        track_lengths.append(track_length)
        sample_count = max(sample_count, len(fraction) - 1)
        geodetic = pair["ibt"].get("geodetic", {})
        latitude = np.asarray(geodetic.get("latitudeDeg", []), dtype=np.float64)
        longitude = np.asarray(geodetic.get("longitudeDeg", []), dtype=np.float64)
        altitude = np.asarray(geodetic.get("altitudeM", []), dtype=np.float64)
        if (
            len(latitude) < 2
            or len(latitude) != len(longitude)
            or len(latitude) != len(altitude)
        ):
            raise ValueError("Matched IBT does not contain valid geodetic samples")
        all_latitude.append(latitude)
        all_longitude.append(longitude)
        all_altitude.append(altitude)
    primary_track_length = track_lengths[0]
    if any(
        abs(value - primary_track_length) > max_track_length_difference_m
        for value in track_lengths[1:]
    ):
        raise ValueError("Matched BLAP track lengths do not agree")
    if sample_count < 3:
        raise ValueError("Matched reconstruction grid is too short")

    joined_latitude = np.concatenate(all_latitude)
    joined_longitude = np.concatenate(all_longitude)
    joined_altitude = np.concatenate(all_altitude)
    if float(np.ptp(joined_latitude)) > 1.0 or float(np.ptp(joined_longitude)) > 1.0:
        raise ValueError("Matched IBT coordinates do not describe one local track")
    origin = {
        "latitudeDeg": float(np.mean(joined_latitude)),
        "longitudeDeg": float(np.mean(joined_longitude)),
        "altitudeM": float(np.mean(joined_altitude)),
    }

    grid = np.linspace(0.0, 1.0, sample_count, endpoint=False)
    positions = []
    offsets = []
    for pair, (fraction, offset) in zip(pairs, lateral_series):
        geodetic = pair["ibt"]["geodetic"]
        enu = _geodetic_to_enu(
            geodetic["latitudeDeg"],
            geodetic["longitudeDeg"],
            geodetic["altitudeM"],
            origin,
        )
        ibt_fraction = pair["ibt"]["lapFraction"]
        positions.append(
            np.column_stack(
                (
                    _periodic_interp(grid, ibt_fraction, enu[:, 0]),
                    _periodic_interp(grid, ibt_fraction, enu[:, 1]),
                    _periodic_interp(grid, ibt_fraction, enu[:, 2]),
                )
            )
        )
        offsets.append(_periodic_interp(grid, fraction, offset))
    positions = np.asarray(positions, dtype=np.float64)
    offsets = np.asarray(offsets, dtype=np.float64)

    offset_mean = np.mean(offsets, axis=0)
    centered_offset = offsets - offset_mean
    denominator = np.sum(centered_offset * centered_offset, axis=0)
    position_mean = np.mean(positions[:, :, :2], axis=0)
    centered_position = positions[:, :, :2] - position_mean
    slope = np.sum(
        centered_offset[:, :, None] * centered_position,
        axis=0,
    ) / np.maximum(denominator[:, None], 1e-12)
    separation = np.max(offsets, axis=0) - np.min(offsets, axis=0)
    slope_length = np.linalg.norm(slope, axis=1)
    usable = (
        (separation >= min_lateral_separation_m)
        & (denominator > 1e-12)
        & (slope_length > 1e-6)
    )
    usable_fraction = float(np.mean(usable))
    if usable_fraction < min_usable_fraction or int(np.sum(usable)) < 3:
        raise ValueError(
            "Matched laps have usable lateral separation over only {:.1%}; "
            "required {:.1%}".format(usable_fraction, min_usable_fraction)
        )

    indices = np.arange(sample_count, dtype=np.float64)
    usable_indices = indices[usable]
    normals = np.empty((sample_count, 2), dtype=np.float64)
    for axis in range(2):
        values = slope[usable, axis]
        normals[:, axis] = np.interp(
            indices,
            np.concatenate(
                (
                    usable_indices[-1:] - sample_count,
                    usable_indices,
                    usable_indices[:1] + sample_count,
                )
            ),
            np.concatenate((values[-1:], values, values[:1])),
        )
    grid_spacing = primary_track_length / sample_count
    smoothing_window = max(
        1,
        int(round(smoothing_distance_m / max(grid_spacing, 1e-12))),
    )
    normals = _circular_moving_average(normals, smoothing_window)
    normal_length = np.linalg.norm(normals, axis=1)
    normals = normals / np.maximum(normal_length[:, None], 1e-12)

    axis_xy = np.mean(
        positions[:, :, :2] - offsets[:, :, None] * normals[None, :, :],
        axis=0,
    )
    for _iteration in range(max_iterations):
        axis_xy = _circular_moving_average(axis_xy, smoothing_window)
        tangent = np.roll(axis_xy, -1, axis=0) - np.roll(axis_xy, 1, axis=0)
        tangent_length = np.linalg.norm(tangent, axis=1)
        tangent = tangent / np.maximum(tangent_length[:, None], 1e-12)
        geometric_normal = np.column_stack((-tangent[:, 1], tangent[:, 0]))
        if float(np.median(np.sum(geometric_normal[usable] * slope[usable], axis=1))) < 0:
            geometric_normal *= -1.0
        normals = geometric_normal
        axis_xy = np.mean(
            positions[:, :, :2] - offsets[:, :, None] * normals[None, :, :],
            axis=0,
        )
    axis_xy = _circular_moving_average(axis_xy, smoothing_window)
    tangent = np.roll(axis_xy, -1, axis=0) - np.roll(axis_xy, 1, axis=0)
    tangent = tangent / np.maximum(
        np.linalg.norm(tangent, axis=1)[:, None],
        1e-12,
    )
    normals = np.column_stack((-tangent[:, 1], tangent[:, 0]))
    if float(np.median(np.sum(normals[usable] * slope[usable], axis=1))) < 0:
        normals *= -1.0

    predicted = axis_xy[None, :, :] + offsets[:, :, None] * normals[None, :, :]
    residual = np.linalg.norm(positions[:, :, :2] - predicted, axis=2)
    axis_up = _circular_moving_average(
        np.mean(positions[:, :, 2], axis=0),
        smoothing_window,
    )
    closed_step = np.roll(axis_xy, -1, axis=0) - axis_xy
    axis_length = float(np.sum(np.linalg.norm(closed_step, axis=1)))
    slope_scale = slope_length[usable]
    closed_fraction = np.linspace(0.0, 1.0, sample_count + 1)
    primary_template = pairs[0]["template"]
    return {
        "lapFraction": closed_fraction.tolist(),
        "eastM": np.concatenate((axis_xy[:, 0], axis_xy[:1, 0])).tolist(),
        "northM": np.concatenate((axis_xy[:, 1], axis_xy[:1, 1])).tolist(),
        "upM": np.concatenate((axis_up, axis_up[:1])).tolist(),
        "referenceKind": "matched-blap-ibt-least-squares",
        "trackLengthM": primary_track_length,
        "origin": origin,
        "metadata": {
            "trackName": primary_template.get("header", {}).get("trackName", ""),
            "carName": primary_template.get("header", {}).get("carShortName", ""),
            "referenceCount": len(pairs),
        },
        "diagnostics": {
            "pairCount": len(pairs),
            "sampleCount": sample_count,
            "usableFraction": usable_fraction,
            "minimumLateralSeparationM": min_lateral_separation_m,
            "lateralSeparationMedianM": float(np.median(separation)),
            "lateralSeparationMaxM": float(np.max(separation)),
            "normalSlopeScaleMedian": float(np.median(slope_scale)),
            "normalSlopeScaleP95Error": float(
                np.percentile(np.abs(slope_scale - 1.0), 95)
            ),
            "fitRmseM": float(np.sqrt(np.mean(residual * residual))),
            "fitP95M": float(np.percentile(residual, 95)),
            "fitMaxM": float(np.max(residual)),
            "axisLengthM": axis_length,
            "axisLengthDifferenceM": axis_length - primary_track_length,
            "smoothingDistanceM": smoothing_distance_m,
            "iterations": max_iterations,
        },
    }


def fit_ibt_distance_map(
    points: Sequence[Dict[str, Any]],
    reference: Dict[str, Any],
    target_track_length_m: float,
    sample_spacing_m: float = 2.0,
    max_iterations: int = 30,
    rejection_distance_m: float = 12.0,
    convergence_tolerance: float = 1e-8,
    allow_scale: bool = True,
    smoothing_distance_m: float = 60.0,
    endpoint_weight: float = 1000000.0,
    monotonic_blend: float = 0.01,
    lateral_smoothing_distance_m: float = 8.0,
) -> Dict[str, Any]:
    """Fit source distance and signed lateral offset to a reference path."""
    if len(points) < 2:
        raise ValueError("At least two source points are required for IBT alignment")
    if target_track_length_m <= 0 or sample_spacing_m <= 0:
        raise ValueError("Track length and alignment spacing must be positive")
    if max_iterations <= 0:
        raise ValueError("ICP iteration count must be positive")
    if rejection_distance_m <= 0 or convergence_tolerance <= 0:
        raise ValueError("ICP rejection distance and tolerance must be positive")
    if smoothing_distance_m < 0 or lateral_smoothing_distance_m < 0 or endpoint_weight <= 0:
        raise ValueError("Alignment smoothing and endpoint weight are invalid")
    if not 0.0 < monotonic_blend <= 1.0:
        raise ValueError("Monotonic blend must be in (0, 1]")

    source_xyz, source_distances = _cumulative_distances(points)
    source_total = float(source_distances[-1])
    source_grid = np.linspace(
        0.0,
        source_total,
        max(2, int(math.ceil(source_total / sample_spacing_m)) + 1),
    )
    source_path = np.column_stack(
        [
            np.interp(source_grid, source_distances, source_xyz[:, 0]),
            np.interp(source_grid, source_distances, source_xyz[:, 1]),
        ]
    )

    fraction = np.asarray(reference["lapFraction"], dtype=np.float64)
    reference_x = np.asarray(reference["eastM"], dtype=np.float64)
    reference_y = np.asarray(reference["northM"], dtype=np.float64)
    target_grid = np.linspace(
        0.0,
        target_track_length_m,
        max(2, int(math.ceil(target_track_length_m / sample_spacing_m)) + 1),
    )
    target_fraction = target_grid / target_track_length_m
    reference_path = np.column_stack(
        [
            np.interp(target_fraction, fraction, reference_x),
            np.interp(target_fraction, fraction, reference_y),
        ]
    )

    paired_fraction = np.linspace(0.0, 1.0, max(len(source_path), len(reference_path)))
    paired_source = np.column_stack(
        [
            np.interp(paired_fraction, source_grid / source_total, source_path[:, 0]),
            np.interp(paired_fraction, source_grid / source_total, source_path[:, 1]),
        ]
    )
    paired_reference = np.column_stack(
        [
            np.interp(
                paired_fraction,
                target_grid / target_track_length_m,
                reference_path[:, 0],
            ),
            np.interp(
                paired_fraction,
                target_grid / target_track_length_m,
                reference_path[:, 1],
            ),
        ]
    )
    scale, rotation, translation = _fit_similarity(
        paired_source, paired_reference, allow_scale
    )
    iteration_count = 0
    for iteration_count in range(1, max_iterations + 1):
        transformed = scale * source_path @ rotation + translation
        projected, residuals, _segments, _fractions = _project_polyline(
            transformed, reference_path
        )
        accepted = residuals <= rejection_distance_m
        if int(np.sum(accepted)) < 3:
            raise ValueError("ICP rejected too many source samples")
        next_scale, next_rotation, next_translation = _fit_similarity(
            source_path[accepted], projected[accepted], allow_scale
        )
        delta = max(
            abs(next_scale - scale),
            float(np.max(np.abs(next_rotation - rotation))),
            float(np.max(np.abs(next_translation - translation))),
        )
        scale, rotation, translation = next_scale, next_rotation, next_translation
        if delta < convergence_tolerance:
            break

    transformed = scale * source_path @ rotation + translation
    projected, residuals, segments, segment_fractions = _project_polyline(
        transformed, reference_path
    )
    segment_start = target_grid[segments]
    segment_end = target_grid[segments + 1]
    projected_target = segment_start + segment_fractions * (segment_end - segment_start)
    expected_target = source_grid / source_total * target_track_length_m
    projected_target += (
        np.round((expected_target - projected_target) / target_track_length_m)
        * target_track_length_m
    )
    accepted = residuals <= rejection_distance_m
    if int(np.sum(accepted)) < 2:
        raise ValueError("IBT projection has insufficient accepted samples")
    correction = projected_target - expected_target
    correction = np.interp(source_grid, source_grid[accepted], correction[accepted])
    smoothing_window = max(
        1, int(round(smoothing_distance_m / max(sample_spacing_m, 1e-12)))
    )
    correction = _moving_average(correction, smoothing_window)
    fraction = source_grid / source_total
    endpoint_trend = correction[0] + fraction * (correction[-1] - correction[0])
    correction = correction - endpoint_trend
    correction[0] = 0.0
    correction[-1] = 0.0
    mapped_target = expected_target + correction
    mapped_target[0] = 0.0
    mapped_target[-1] = target_track_length_m
    mapped_target = _isotonic(mapped_target, endpoint_weight)
    isotonic_span = float(mapped_target[-1] - mapped_target[0])
    if isotonic_span <= 0:
        raise ValueError("Reference distance mapping collapsed to zero extent")
    mapped_target = (
        (mapped_target - mapped_target[0])
        * (target_track_length_m / isotonic_span)
    )
    mapped_target = (
        (1.0 - monotonic_blend) * mapped_target
        + monotonic_blend * expected_target
    )
    mapped_target[0] = 0.0
    mapped_target[-1] = target_track_length_m
    mapped_step = np.diff(mapped_target)
    if np.any(mapped_step <= 0):
        failing_index = int(np.argmin(mapped_step))
        raise ValueError(
            "Reference distance mapping is not strictly monotonic at {} "
            "(step {:.12g}m)".format(failing_index, mapped_step[failing_index])
        )

    tangent = reference_path[segments + 1] - reference_path[segments]
    tangent_length = np.linalg.norm(tangent, axis=1)
    tangent = tangent / np.maximum(tangent_length[:, None], 1e-12)
    left_normal = np.column_stack((-tangent[:, 1], tangent[:, 0]))
    lateral_offset = np.sum((transformed - projected) * left_normal, axis=1)
    lateral_window = max(
        1,
        int(round(lateral_smoothing_distance_m / max(sample_spacing_m, 1e-12))),
    )
    lateral_offset = _moving_average(lateral_offset, lateral_window)

    angle = math.degrees(math.atan2(rotation[0, 1], rotation[0, 0]))
    return {
        "targetDistanceM": mapped_target.tolist(),
        "sourceDistanceM": source_grid.tolist(),
        "lateralOffsetM": lateral_offset.tolist(),
        "headingRotationDeg": angle,
        "diagnostics": {
            "iterations": iteration_count,
            "scale": scale,
            "rotationDeg": angle,
            "rmseM": float(np.sqrt(np.mean(residuals * residuals))),
            "meanResidualM": float(np.mean(residuals)),
            "p95ResidualM": float(np.percentile(residuals, 95)),
            "maxResidualM": float(np.max(residuals)),
            "sourceTrackLengthM": source_total,
            "targetTrackLengthM": target_track_length_m,
            "correctionMinM": float(np.min(mapped_target - expected_target)),
            "correctionMaxM": float(np.max(mapped_target - expected_target)),
            "lateralOffsetMinM": float(np.min(lateral_offset)),
            "lateralOffsetMaxM": float(np.max(lateral_offset)),
        },
    }


def combine_alignment_maps(
    longitudinal: Dict[str, Any],
    lateral: Dict[str, Any],
) -> Dict[str, Any]:
    """Use one fit for distance correspondence and another for BLAP offset."""
    source = np.asarray(longitudinal["sourceDistanceM"], dtype=np.float64)
    lateral_source = np.asarray(lateral["sourceDistanceM"], dtype=np.float64)
    lateral_value = np.asarray(lateral["lateralOffsetM"], dtype=np.float64)
    output = dict(longitudinal)
    output["lateralOffsetM"] = np.interp(
        source,
        lateral_source,
        lateral_value,
    ).tolist()
    output["headingRotationDeg"] = float(lateral["headingRotationDeg"])
    output["diagnostics"] = {
        "longitudinal": longitudinal.get("diagnostics", {}),
        "lateral": lateral.get("diagnostics", {}),
    }
    return output
