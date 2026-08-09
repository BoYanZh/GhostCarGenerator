"""MoTeC LD loading isolated from the optional ldparser dependency."""

import bisect
import importlib.util
import math
import statistics
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


CHANNEL_ALIASES = {
    "latitude": ("GPS Latitude", "GPS_Lat", "Latitude", "Pos_Lat"),
    "longitude": ("GPS Longitude", "GPS_Long", "Longitude", "Pos_Long"),
    "altitude": ("GPS Altitude", "GPS_Alt", "Altitude", "Pos_Alt"),
    "x": ("Position X", "Pos X"),
    "y": ("Position Y", "Pos Y"),
    "z": ("Position Z", "Pos Z"),
    "speed": ("Ground Speed", "GPS Speed", "Speed", "Veh_Speed"),
    "heading": ("GPS Heading", "Heading", "Yaw"),
    "pitch": ("Pitch", "GPS Pitch"),
    "roll": ("Roll", "GPS Roll"),
    "gear": ("Gear", "n_Gear"),
    "brake": ("Brake Pos", "Brake_Pos", "Brake", "Brake Press FL", "Brake_Press"),
    "throttle": ("Throttle Pos", "Throttle_Pos", "Throttle", "Accel_Pos"),
    "lap": ("Lap Number", "Lap_Num", "Lap"),
    "time": ("Running Time", "Time", "Session Time"),
}


def load_ld_data(parser_path: Optional[str] = None):
    candidates = []
    if parser_path:
        supplied = Path(parser_path).expanduser().resolve()
        candidates.append(supplied / "ldparser.py" if supplied.is_dir() else supplied)
    package_root = Path(__file__).resolve().parent
    candidates.extend(
        (
            package_root.parent / "ldparser" / "ldparser.py",
            package_root / "ldparser" / "ldparser.py",
        )
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location("_ghost_car_ldparser", str(candidate))
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if hasattr(module, "ldData"):
            return module.ldData
    try:
        from ldparser.ldparser import ldData
        return ldData
    except (ImportError, AttributeError):
        pass
    raise ImportError(
        "Unable to load ldparser. Initialize the git submodule or pass --ldparser-path."
    )


def parse_channel_overrides(items: Sequence[str]) -> Dict[str, str]:
    result = {}
    for item in items:
        if "=" not in item:
            raise ValueError("--channel values must use ROLE=CHANNEL_NAME")
        role, name = item.split("=", 1)
        role = role.strip().lower()
        if role not in CHANNEL_ALIASES:
            raise ValueError(
                "Unknown channel role {!r}; choose from {}".format(
                    role, ", ".join(sorted(CHANNEL_ALIASES))
                )
            )
        if not name.strip():
            raise ValueError("Channel name cannot be empty")
        result[role] = name.strip()
    return result


def _channel_map(ld: Any) -> Dict[str, Any]:
    return {str(channel.name).strip().casefold(): channel for channel in ld.channs}


def _find_channel(
    channels: Dict[str, Any],
    role: str,
    overrides: Dict[str, str],
):
    names = (overrides[role],) if role in overrides else CHANNEL_ALIASES[role]
    for name in names:
        channel = channels.get(name.casefold())
        if channel is not None:
            return channel
    return None


def _channel_data(channel: Any):
    return None if channel is None else channel.data


def _speed_to_mps(value: float, unit: str) -> float:
    normalized = unit.strip().casefold().replace(" ", "")
    if normalized in ("km/h", "kmh", "kph"):
        return value / 3.6
    if normalized in ("mph", "mi/h"):
        return value * 0.44704
    if normalized in ("m/s", "mps", "ms-1", ""):
        return value
    raise ValueError("Unsupported speed unit {!r}".format(unit))


def _metadata_value(head: Any, *names: str) -> str:
    for name in names:
        value = getattr(head, name, "")
        if value:
            return str(value)
    return ""


def _load_ldx_lap_intervals(
    ld_path: str,
    ldx_path: Optional[str],
    use_companion_ldx: bool,
    time_scale: float,
) -> List[Dict[str, float]]:
    if time_scale <= 0:
        raise ValueError("LDX time scale must be positive")
    explicit = Path(ldx_path).expanduser() if ldx_path else None
    candidate = explicit or Path(ld_path).expanduser().with_suffix(".ldx")
    if explicit is None and not use_companion_ldx:
        return []
    if not candidate.is_file():
        if explicit is not None:
            raise FileNotFoundError("LDX file not found: {}".format(candidate))
        return []
    root = ElementTree.parse(str(candidate)).getroot()
    preferred_marker_times = []
    fallback_marker_times = []
    for group in root.findall(".//MarkerGroup"):
        if str(group.get("Name", "")).casefold() != "beacons":
            continue
        for marker in group.findall("Marker"):
            value = marker.get("Time")
            if value is None:
                continue
            marker_time = float(value) / time_scale
            fallback_marker_times.append(marker_time)
            if str(marker.get("Name", "")).casefold() == "start/finish":
                preferred_marker_times.append(marker_time)
    marker_times = (
        preferred_marker_times
        if len(preferred_marker_times) >= 2
        else fallback_marker_times
    )
    marker_times = sorted(set(marker_times))
    return [
        {
            "lap": index + 1,
            "startS": start,
            "endS": end,
            "durationS": end - start,
        }
        for index, (start, end) in enumerate(zip(marker_times, marker_times[1:]))
        if end > start
    ]


def _time_descriptor(times: Sequence[float], target: float) -> Tuple[int, int, float]:
    right = bisect.bisect_left(times, target)
    if right <= 0:
        return 0, 0, 0.0
    if right >= len(times):
        last = len(times) - 1
        return last, last, 0.0
    left = right - 1
    span = float(times[right]) - float(times[left])
    fraction = (target - float(times[left])) / span if span else 0.0
    return left, right, max(0.0, min(1.0, fraction))


def _sample_series(
    values: Optional[Sequence[float]],
    descriptor: Tuple[int, int, float, float],
    default: Optional[float] = None,
) -> Optional[float]:
    if values is None:
        return default
    left, right, fraction, _ = descriptor
    first = float(values[left])
    if left == right:
        return first
    return first + fraction * (float(values[right]) - first)


def extract_motec_points(
    ld_path: str,
    parser_path: Optional[str] = None,
    channel_overrides: Optional[Dict[str, str]] = None,
    target_lap: Optional[int] = None,
    lap_selection: str = "fastest",
    min_lap_seconds: float = 0.0,
    max_lap_seconds: float = float("inf"),
    min_lap_distance_ratio: float = 0.9,
    max_gps_step_m: Optional[float] = None,
    gps_step_outlier_factor: float = 20.0,
    speed_unit: str = "auto",
    heading_unit: str = "degrees",
    origin_latitude: Optional[float] = None,
    origin_longitude: Optional[float] = None,
    origin_altitude: Optional[float] = None,
    earth_radius_m: float = 6371008.8,
    ldx_path: Optional[str] = None,
    use_companion_ldx: bool = True,
    ldx_time_scale: float = 1000000.0,
) -> Dict[str, Any]:
    ld_data = load_ld_data(parser_path)
    ld = ld_data.fromfile(str(Path(ld_path).expanduser()))
    channels = _channel_map(ld)
    overrides = channel_overrides or {}
    selected_channels = {
        role: _find_channel(channels, role, overrides)
        for role in CHANNEL_ALIASES
    }
    latitude = _channel_data(selected_channels["latitude"])
    longitude = _channel_data(selected_channels["longitude"])
    cartesian_x = _channel_data(selected_channels["x"])
    cartesian_y = _channel_data(selected_channels["y"])
    cartesian_z = _channel_data(selected_channels["z"])
    uses_geodetic = latitude is not None and longitude is not None
    uses_cartesian = cartesian_x is not None and cartesian_y is not None
    if not uses_geodetic and not uses_cartesian:
        raise ValueError(
            "MoTeC log requires latitude/longitude or configured x/y position channels"
        )
    if uses_geodetic:
        sample_count = min(len(latitude), len(longitude))
        position_frequency_channel = selected_channels["latitude"]
    else:
        coordinate_series = [cartesian_x, cartesian_y]
        if cartesian_z is not None:
            coordinate_series.append(cartesian_z)
        sample_count = min(len(series) for series in coordinate_series)
        position_frequency_channel = selected_channels["x"]
    if earth_radius_m <= 0:
        raise ValueError("Earth radius must be positive")
    if not 0.0 < min_lap_distance_ratio <= 1.0:
        raise ValueError("Minimum lap distance ratio must be in (0, 1]")
    if max_gps_step_m is not None and max_gps_step_m <= 0:
        raise ValueError("Maximum GPS step must be positive")
    if gps_step_outlier_factor <= 0:
        raise ValueError("GPS step outlier factor must be positive")
    gps_steps = []
    for previous in range(sample_count - 1):
        current = previous + 1
        if uses_geodetic:
            mean_latitude = math.radians(
                (float(latitude[previous]) + float(latitude[current])) * 0.5
            )
            dx = (
                math.radians(float(longitude[current]) - float(longitude[previous]))
                * earth_radius_m
                * math.cos(mean_latitude)
            )
            dy = (
                math.radians(float(latitude[current]) - float(latitude[previous]))
                * earth_radius_m
            )
            gps_steps.append(math.hypot(dx, dy))
        else:
            dx = float(cartesian_x[current]) - float(cartesian_x[previous])
            dy = float(cartesian_y[current]) - float(cartesian_y[previous])
            dz = (
                float(cartesian_z[current]) - float(cartesian_z[previous])
                if cartesian_z is not None
                else 0.0
            )
            gps_steps.append(math.sqrt(dx * dx + dy * dy + dz * dz))
    positive_steps = [distance for distance in gps_steps if distance > 0]
    automatic_step_limit = (
        statistics.median(positive_steps) * gps_step_outlier_factor
        if positive_steps
        else float("inf")
    )
    step_limit = max_gps_step_m if max_gps_step_m is not None else automatic_step_limit
    frequency = float(getattr(position_frequency_channel, "freq", 0.0))
    if frequency <= 0:
        raise ValueError("Position channel frequency must be positive")
    time_step = 1.0 / frequency
    lap_values = _channel_data(selected_channels["lap"])
    running_time = _channel_data(selected_channels["time"])
    indices = list(range(sample_count))
    selected_lap = None
    selection_source = "all"
    ldx_interval = None
    if lap_selection != "all":
        intervals = _load_ldx_lap_intervals(
            ld_path,
            ldx_path,
            use_companion_ldx,
            ldx_time_scale,
        )
        valid_intervals = [
            interval
            for interval in intervals
            if min_lap_seconds <= interval["durationS"] <= max_lap_seconds
        ]
        if valid_intervals:
            if target_lap is not None:
                ldx_interval = next(
                    (item for item in valid_intervals if int(item["lap"]) == target_lap),
                    None,
                )
            else:
                ldx_interval = (
                    min(valid_intervals, key=lambda item: item["durationS"])
                    if lap_selection == "fastest"
                    else valid_intervals[0]
                )
        if ldx_interval is not None:
            selected_lap = int(ldx_interval["lap"])
            selection_source = "ldx"
    if ldx_interval is None and lap_values is not None and len(lap_values) >= sample_count:
        laps = sorted({int(value) for value in lap_values[:sample_count] if int(value) > 0})
        if target_lap is not None:
            if target_lap not in laps:
                raise ValueError("Requested lap {} is not present".format(target_lap))
            selected_lap = target_lap
        elif lap_selection != "all":
            candidates = []
            for lap in laps:
                lap_indices = [index for index in indices if int(lap_values[index]) == lap]
                duration = len(lap_indices) * time_step
                distance = 0.0
                for previous, current in zip(lap_indices, lap_indices[1:]):
                    if current == previous + 1 and gps_steps[previous] <= step_limit:
                        distance += gps_steps[previous]
                if min_lap_seconds <= duration <= max_lap_seconds:
                    candidates.append((duration, lap, distance))
            if candidates:
                longest = max(candidate[2] for candidate in candidates)
                complete = [
                    candidate
                    for candidate in candidates
                    if candidate[2] >= longest * min_lap_distance_ratio
                ]
                selected_lap = (
                    min(complete)[1] if lap_selection == "fastest" else complete[0][1]
                )
                selection_source = "lap-channel"
        if selected_lap is not None:
            indices = [index for index in indices if int(lap_values[index]) == selected_lap]
    descriptors = []
    if ldx_interval is not None:
        if running_time is None:
            times = [index * time_step for index in range(sample_count)]
        else:
            if len(running_time) < sample_count:
                raise ValueError("Running-time channel is shorter than position channels")
            times = [float(value) for value in running_time[:sample_count]]
        if any(current < previous for previous, current in zip(times, times[1:])):
            raise ValueError("Running-time channel must be monotonic for LDX selection")
        start_time = float(ldx_interval["startS"])
        end_time = float(ldx_interval["endS"])
        start_left, start_right, start_fraction = _time_descriptor(times, start_time)
        end_left, end_right, end_fraction = _time_descriptor(times, end_time)
        descriptors.append((start_left, start_right, start_fraction, 0.0))
        descriptors.extend(
            (index, index, 0.0, times[index] - start_time)
            for index in range(start_right, end_right + 1)
            if start_time < times[index] < end_time
        )
        descriptors.append(
            (end_left, end_right, end_fraction, end_time - start_time)
        )
    else:
        descriptors = [
            (source_index, source_index, 0.0, output_index * time_step)
            for output_index, source_index in enumerate(indices)
        ]
    if len(descriptors) < 2:
        raise ValueError("Selected MoTeC data contains fewer than two samples")

    altitude = _channel_data(selected_channels["altitude"])
    speed = _channel_data(selected_channels["speed"])
    heading = _channel_data(selected_channels["heading"])
    pitch = _channel_data(selected_channels["pitch"])
    roll = _channel_data(selected_channels["roll"])
    gear = _channel_data(selected_channels["gear"])
    brake = _channel_data(selected_channels["brake"])
    throttle = _channel_data(selected_channels["throttle"])
    first_descriptor = descriptors[0]
    if uses_geodetic:
        first_latitude = float(_sample_series(latitude, first_descriptor, 0.0))
        first_longitude = float(_sample_series(longitude, first_descriptor, 0.0))
        lat0 = first_latitude if origin_latitude is None else origin_latitude
        lon0 = first_longitude if origin_longitude is None else origin_longitude
        first_altitude = float(_sample_series(altitude, first_descriptor, 0.0))
        alt0 = first_altitude if origin_altitude is None else origin_altitude
        cos_latitude = math.cos(math.radians(lat0))
        coordinate_origin = {
            "latitudeDeg": float(lat0),
            "longitudeDeg": float(lon0),
            "altitudeM": float(alt0),
        }
    else:
        x0 = float(_sample_series(cartesian_x, first_descriptor, 0.0))
        y0 = float(_sample_series(cartesian_y, first_descriptor, 0.0))
        z0 = float(_sample_series(cartesian_z, first_descriptor, 0.0))
        coordinate_origin = {"xM": x0, "yM": y0, "zM": z0}
    detected_speed_unit = ""
    if speed is not None:
        detected_speed_unit = str(getattr(selected_channels["speed"], "unit", "") or "")
    selected_speed_unit = detected_speed_unit if speed_unit == "auto" else speed_unit
    points = []
    for descriptor in descriptors:
        if uses_geodetic:
            lat = float(_sample_series(latitude, descriptor, lat0))
            lon = float(_sample_series(longitude, descriptor, lon0))
            alt = float(_sample_series(altitude, descriptor, alt0))
            x_m = math.radians(lon - lon0) * earth_radius_m * cos_latitude
            y_m = math.radians(lat - lat0) * earth_radius_m
            z_m = alt - alt0
        else:
            x_m = float(_sample_series(cartesian_x, descriptor, x0)) - x0
            y_m = float(_sample_series(cartesian_y, descriptor, y0)) - y0
            z_m = float(_sample_series(cartesian_z, descriptor, z0)) - z0
        speed_value = _sample_series(speed, descriptor, 0.0)
        point = {
            "timeS": descriptor[3],
            "xM": x_m,
            "yM": y_m,
            "zM": z_m,
            "speedMS": _speed_to_mps(float(speed_value), selected_speed_unit)
            if speed is not None
            else 0.0,
            "brake": float(_sample_series(brake, descriptor, 0.0)),
            "throttle": float(_sample_series(throttle, descriptor, 0.0)),
            "gear": _sample_series(gear, descriptor, None),
            "pitchRad": float(_sample_series(pitch, descriptor, 0.0)),
            "rollRad": float(_sample_series(roll, descriptor, 0.0)),
        }
        if heading is not None:
            heading_value = float(_sample_series(heading, descriptor, 0.0))
            point["headingRad"] = (
                math.radians(heading_value)
                if heading_unit == "degrees"
                else heading_value
            )
        points.append(point)
    return {
        "points": points,
        "selectedLap": selected_lap,
        "selectionSource": selection_source,
        "lapTimeS": float(points[-1]["timeS"]),
        "frequencyHz": frequency,
        "origin": coordinate_origin,
        "coordinateSystem": "geodetic" if uses_geodetic else "cartesian",
        "metadata": {
            "driverName": _metadata_value(ld.head, "driver"),
            "carName": _metadata_value(ld.head, "vehicleid", "vehicle"),
            "trackName": _metadata_value(ld.head, "venue"),
        },
    }
