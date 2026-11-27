"""Unified GPS telemetry extraction for GhostCarGenerator.

Normalizes MoTeC ``.ld``, iRacing ``.ibt``, RaceChrono/AiM/generic ``.csv``,
RaceChrono ``.rcz``, and AiM ``.xrk``/``.xrz`` (when ``libxrk`` is installed)
into the canonical point-set dictionary consumed by ``ac_track`` and
``runtime_ghost``.
"""
from __future__ import annotations

__all__ = [
    "SUPPORTED_TELEMETRY_EXTENSIONS",
    "extract_telemetry_points",
    "is_telemetry_path",
]

import csv
import io
import json
import math
import mmap
import statistics
import struct
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from . import motec

EARTH_RADIUS_M = 6371008.8
SUPPORTED_TELEMETRY_EXTENSIONS = (".ld", ".ibt", ".csv", ".rcz", ".xrk", ".xrz")

_IBT_TYPES = {
    0: "i1",
    1: "i1",
    2: "<i4",
    3: "<u4",
    4: "<f4",
    5: "<f8",
}


def is_telemetry_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_TELEMETRY_EXTENSIONS


def _build_geodetic_point_set(
    *,
    times_s: Sequence[float],
    latitudes: Sequence[float],
    longitudes: Sequence[float],
    altitudes: Optional[Sequence[float]] = None,
    speeds_ms: Optional[Sequence[float]] = None,
    steers_rad: Optional[Sequence[float]] = None,
    throttles: Optional[Sequence[float]] = None,
    brakes: Optional[Sequence[float]] = None,
    gears: Optional[Sequence[float]] = None,
    rpms: Optional[Sequence[float]] = None,
    headings_rad: Optional[Sequence[float]] = None,
    selected_lap: Optional[int] = 1,
    selection_source: str = "auto",
    metadata: Optional[Mapping[str, Any]] = None,
    earth_radius_m: float = EARTH_RADIUS_M,
) -> Dict[str, Any]:
    t = np.asarray(times_s, dtype=np.float64)
    lat = np.asarray(latitudes, dtype=np.float64)
    lon = np.asarray(longitudes, dtype=np.float64)
    n = min(len(t), len(lat), len(lon))
    if n < 3:
        raise ValueError("Telemetry lap contains fewer than 3 samples")
    t = t[:n] - float(t[0])
    lat = lat[:n]
    lon = lon[:n]
    alt = (
        np.asarray(altitudes[:n], dtype=np.float64)
        if altitudes is not None and len(altitudes) >= n
        else np.zeros(n, dtype=np.float64)
    )
    valid = np.isfinite(t) & np.isfinite(lat) & np.isfinite(lon) & np.isfinite(alt)
    if int(valid.sum()) < 3:
        raise ValueError("Telemetry lap contains fewer than 3 finite GPS samples")
    idx = np.where(valid)[0]
    t = t[idx]
    lat = lat[idx]
    lon = lon[idx]
    alt = alt[idx]

    dts = np.diff(t)
    pos_dts = dts[dts > 1e-6]
    freq = float(1.0 / np.median(pos_dts)) if len(pos_dts) > 0 else 20.0

    lat0 = float(lat[0])
    lon0 = float(lon[0])
    alt0 = float(alt[0])
    cos_lat = math.cos(math.radians(lat0))

    def _pick(seq: Optional[Sequence[float]], i: int, default: Any) -> Any:
        if seq is None or i >= len(seq):
            return default
        val = seq[i]
        if val is None or (isinstance(val, float) and not math.isfinite(val)):
            return default
        return float(val)

    points: List[Dict[str, Any]] = []
    for out_i, src_i in enumerate(idx):
        si = int(src_i)
        x_m = math.radians(float(lon[out_i]) - lon0) * earth_radius_m * cos_lat
        y_m = math.radians(float(lat[out_i]) - lat0) * earth_radius_m
        z_m = float(alt[out_i]) - alt0
        pt: Dict[str, Any] = {
            "timeS": float(t[out_i]),
            "xM": x_m,
            "yM": y_m,
            "zM": z_m,
            "speedMS": _pick(speeds_ms, si, 0.0),
            "brake": _pick(brakes, si, 0.0),
            "throttle": _pick(throttles, si, 0.0),
            "gear": _pick(gears, si, None),
            "rpm": _pick(rpms, si, None),
            "steerRad": _pick(steers_rad, si, None),
            "pitchRad": 0.0,
            "rollRad": 0.0,
            "lapNumber": int(selected_lap) if selected_lap is not None else 1,
        }
        hdg = _pick(headings_rad, si, None)
        if hdg is not None:
            pt["headingRad"] = hdg
        points.append(pt)

    return {
        "points": points,
        "selectedLap": selected_lap,
        "selectionSource": selection_source,
        "lapTimeS": float(t[-1]),
        "frequencyHz": freq,
        "origin": {
            "latitudeDeg": lat0,
            "longitudeDeg": lon0,
            "altitudeM": alt0,
        },
        "coordinateSystem": "geodetic",
        "sessionSegments": [],
        "metadata": dict(metadata or {}),
    }


def _extract_ibt_points(
    ibt_path: Path,
    *,
    target_lap: Optional[int] = None,
    lap_selection: str = "fastest",
    min_lap_seconds: float = 20.0,
    max_lap_seconds: float = 600.0,
) -> Dict[str, Any]:
    with ibt_path.open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            if len(raw) < 112:
                raise ValueError("IBT file is too small")
            (
                _version,
                _status,
                tick_rate,
                _session_update,
                session_length,
                session_offset,
                variable_count,
                variable_offset,
                _buffer_count,
                buffer_length,
                _pad0,
                _pad1,
            ) = struct.unpack_from("<12i", raw, 0)
            _tick_count, buffer_offset = struct.unpack_from("<2i", raw, 48)
            if tick_rate <= 0 or buffer_length <= 0 or buffer_offset <= 0 or buffer_offset >= len(raw):
                raise ValueError("Invalid IBT header")
            sample_count = (len(raw) - buffer_offset) // buffer_length
            if sample_count < 3:
                raise ValueError("IBT file contains fewer than 3 samples")

            variables: Dict[str, Tuple[np.dtype, int, int]] = {}
            for index in range(variable_count):
                base = variable_offset + index * 144
                if base + 144 > len(raw):
                    break
                value_type, value_offset, value_count = struct.unpack_from("<3i", raw, base)
                name = raw[base + 16 : base + 48].split(b"\0")[0].decode("latin-1")
                dtype = _IBT_TYPES.get(value_type)
                if dtype is not None and value_count == 1:
                    variables[name] = (np.dtype(dtype), value_offset, value_count)

            def _col(name: str, required: bool = False) -> Optional[np.ndarray]:
                info = variables.get(name)
                if info is None:
                    if required:
                        raise ValueError(f"IBT variable {name!r} is missing")
                    return None
                dtype, offset, _count = info
                return np.ndarray(
                    (sample_count,),
                    dtype=dtype,
                    buffer=raw,
                    offset=buffer_offset + offset,
                    strides=(buffer_length,),
                ).astype(np.float64)

            session_time = _col("SessionTime", required=True)
            lap_pct = _col("LapDistPct", required=True)
            lat = _col("Lat", required=True)
            lon = _col("Lon", required=True)
            alt = _col("Alt")
            speed = _col("Speed")
            steer = _col("SteeringWheelAngle")
            throttle = _col("Throttle")
            brake = _col("Brake")
            gear = _col("Gear")
            rpm = _col("RPM")
            yaw_north = _col("YawNorth")
            session_text = raw[session_offset : session_offset + session_length].decode(
                "latin-1", errors="ignore"
            )

    assert session_time is not None and lap_pct is not None and lat is not None and lon is not None
    wraps = np.where((lap_pct[:-1] > 0.90) & (lap_pct[1:] < 0.10))[0] + 1
    candidates: List[Tuple[float, int, int, int]] = []
    for lap_num, (start, end) in enumerate(zip(wraps, wraps[1:]), start=1):
        duration = float(session_time[end] - session_time[start])
        seg_lat = lat[start:end]
        seg_lon = lon[start:end]
        if (
            min_lap_seconds <= duration <= max_lap_seconds
            and end - start >= 3
            and np.all(np.isfinite(seg_lat))
            and np.all(np.isfinite(seg_lon))
            and np.all(np.abs(seg_lat) > 0.01)
            and np.all(np.abs(seg_lon) > 0.01)
        ):
            candidates.append((duration, lap_num, int(start), int(end)))

    if not candidates:
        raise ValueError("IBT file contains no complete valid GPS lap")
    if target_lap is not None:
        chosen = next((c for c in candidates if c[1] == target_lap), None)
        if chosen is None:
            raise ValueError(f"Requested IBT lap {target_lap} not found")
    elif lap_selection == "first":
        chosen = candidates[0]
    else:
        chosen = min(candidates, key=lambda c: c[0])

    _, lap_num, s, e = chosen
    e_inc = min(e + 1, sample_count)

    def _yaml_field(key: str) -> str:
        prefix = key + ":"
        for line in session_text.splitlines():
            st = line.strip()
            if st.startswith(prefix):
                return st.split(":", 1)[1].strip()
        return ""

    return _build_geodetic_point_set(
        times_s=session_time[s:e_inc],
        latitudes=lat[s:e_inc],
        longitudes=lon[s:e_inc],
        altitudes=alt[s:e_inc] if alt is not None else None,
        speeds_ms=speed[s:e_inc] if speed is not None else None,
        steers_rad=steer[s:e_inc] if steer is not None else None,
        throttles=(throttle[s:e_inc] * 100.0) if throttle is not None else None,
        brakes=(brake[s:e_inc] * 100.0) if brake is not None else None,
        gears=gear[s:e_inc] if gear is not None else None,
        rpms=rpm[s:e_inc] if rpm is not None else None,
        headings_rad=yaw_north[s:e_inc] if yaw_north is not None else None,
        selected_lap=lap_num,
        selection_source="ibt",
        metadata={
            "trackName": _yaml_field("TrackName"),
            "carName": _yaml_field("CarPath"),
            "driverName": _yaml_field("UserName"),
        },
    )


def _norm_col(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _find_csv_col(headers: Sequence[str], aliases: Sequence[str]) -> Optional[int]:
    normed = [_norm_col(h) for h in headers]
    for alias in aliases:
        target = _norm_col(alias)
        for idx, col in enumerate(normed):
            if col == target:
                return idx
    for alias in aliases:
        target = _norm_col(alias)
        for idx, col in enumerate(normed):
            if target and target in col:
                return idx
    return None


def _try_float(text: str) -> Optional[float]:
    try:
        val = float(text.strip())
        return val if math.isfinite(val) else None
    except (ValueError, AttributeError):
        return None


def _extract_csv_points_from_text(
    text: str,
    *,
    target_lap: Optional[int] = None,
    lap_selection: str = "fastest",
    min_lap_seconds: float = 0.0,
    max_lap_seconds: float = float("inf"),
    speed_unit: str = "auto",
) -> Dict[str, Any]:
    rows = list(csv.reader(io.StringIO(text)))
    header_idx = None
    for i, row in enumerate(rows[:60]):
        if len(row) < 3:
            continue
        lat_i = _find_csv_col(row, ("latitude", "gpslatitude", "lat", "gpslat"))
        lon_i = _find_csv_col(row, ("longitude", "gpslongitude", "lon", "lng", "gpslon"))
        if lat_i is not None and lon_i is not None:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("CSV does not contain recognizable Latitude/Longitude columns")

    headers = rows[header_idx]
    data_start = header_idx + 1
    units_row: List[str] = []
    if data_start < len(rows):
        probe = rows[data_start]
        lat_i_probe = _find_csv_col(headers, ("latitude", "gpslatitude", "lat", "gpslat"))
        if lat_i_probe is not None and lat_i_probe < len(probe) and _try_float(probe[lat_i_probe]) is None:
            units_row = probe
            data_start += 1

    col_time = _find_csv_col(headers, ("time", "timestamp", "elapsedtime", "sessiontime", "times"))
    col_lat = _find_csv_col(headers, ("latitude", "gpslatitude", "lat", "gpslat"))
    col_lon = _find_csv_col(headers, ("longitude", "gpslongitude", "lon", "lng", "gpslon"))
    col_alt = _find_csv_col(headers, ("altitude", "gpsaltitude", "alt", "elevation"))
    col_spd = _find_csv_col(headers, ("speed", "gpsspeed", "velocity"))
    col_lap = _find_csv_col(headers, ("lapnumber", "lap", "lapid"))
    col_steer = _find_csv_col(headers, ("steeringangle", "steerangle", "steering", "steer"))
    col_thr = _find_csv_col(headers, ("throttle", "throttlepos", "accelerator", "tps"))
    col_brk = _find_csv_col(headers, ("brake", "brakepos", "brakepressure"))
    col_gear = _find_csv_col(headers, ("gear",))
    col_rpm = _find_csv_col(headers, ("rpm", "enginerpm", "enginespeed"))

    assert col_lat is not None and col_lon is not None

    spd_unit_hint = ""
    if col_spd is not None:
        spd_unit_hint = (headers[col_spd] + " " + (units_row[col_spd] if col_spd < len(units_row) else "")).lower()
    steer_unit_hint = ""
    if col_steer is not None:
        steer_unit_hint = (headers[col_steer] + " " + (units_row[col_steer] if col_steer < len(units_row) else "")).lower()

    parsed: List[Dict[str, Any]] = []
    for row_i, row in enumerate(rows[data_start:]):
        if len(row) <= max(col_lat, col_lon):
            continue
        lat_v = _try_float(row[col_lat])
        lon_v = _try_float(row[col_lon])
        if lat_v is None or lon_v is None or (abs(lat_v) < 1e-4 and abs(lon_v) < 1e-4):
            continue
        t_v = _try_float(row[col_time]) if col_time is not None and col_time < len(row) else float(row_i) * 0.1
        if t_v is None:
            continue
        lap_raw = _try_float(row[col_lap]) if col_lap is not None and col_lap < len(row) else None
        spd_raw = _try_float(row[col_spd]) if col_spd is not None and col_spd < len(row) else None
        steer_raw = _try_float(row[col_steer]) if col_steer is not None and col_steer < len(row) else None
        parsed.append(
            {
                "t": t_v,
                "lat": lat_v,
                "lon": lon_v,
                "alt": _try_float(row[col_alt]) if col_alt is not None and col_alt < len(row) else 0.0,
                "spd": spd_raw,
                "lap": int(round(lap_raw)) if lap_raw is not None and lap_raw > 0 else None,
                "steer": steer_raw,
                "thr": _try_float(row[col_thr]) if col_thr is not None and col_thr < len(row) else 0.0,
                "brk": _try_float(row[col_brk]) if col_brk is not None and col_brk < len(row) else 0.0,
                "gear": _try_float(row[col_gear]) if col_gear is not None and col_gear < len(row) else None,
                "rpm": _try_float(row[col_rpm]) if col_rpm is not None and col_rpm < len(row) else None,
            }
        )

    if len(parsed) < 3:
        raise ValueError("CSV contains fewer than 3 valid GPS rows")

    # Select lap if lap column is present
    selected_lap: Optional[int] = 1
    selection_source = "csv-all"
    lap_groups: Dict[int, List[Dict[str, Any]]] = {}
    for item in parsed:
        if item["lap"] is not None:
            lap_groups.setdefault(item["lap"], []).append(item)

    if lap_groups and lap_selection != "all":
        if target_lap is not None:
            if target_lap not in lap_groups:
                raise ValueError(f"Requested CSV lap {target_lap} not found")
            chosen_rows = lap_groups[target_lap]
            selected_lap = target_lap
            selection_source = "csv-lap"
        else:
            candidates = []
            for lap_id, grp in sorted(lap_groups.items()):
                if len(grp) < 10:
                    continue
                dur = float(grp[-1]["t"] - grp[0]["t"])
                if min_lap_seconds <= dur <= max_lap_seconds and dur > 0:
                    # Estimate horizontal path length to filter out-laps / partial laps
                    d_m = 0.0
                    for a, b in zip(grp, grp[1:]):
                        mlat = math.radians(0.5 * (a["lat"] + b["lat"]))
                        dx = math.radians(b["lon"] - a["lon"]) * EARTH_RADIUS_M * math.cos(mlat)
                        dy = math.radians(b["lat"] - a["lat"]) * EARTH_RADIUS_M
                        d_m += math.hypot(dx, dy)
                    candidates.append((dur, lap_id, d_m, grp))
            if candidates:
                longest = max(c[2] for c in candidates)
                complete = [c for c in candidates if c[2] >= longest * 0.9] or candidates
                chosen = (
                    min(complete, key=lambda c: c[0])
                    if lap_selection == "fastest"
                    else complete[0]
                )
                selected_lap = chosen[1]
                chosen_rows = chosen[3]
                selection_source = "csv-lap"
            else:
                chosen_rows = parsed
    else:
        chosen_rows = parsed

    # Convert speed unit
    speeds_ms: Optional[List[float]] = None
    raw_spds = [r["spd"] for r in chosen_rows if r["spd"] is not None]
    if raw_spds:
        unit = speed_unit
        if unit == "auto":
            if "mph" in spd_unit_hint:
                unit = "mph"
            elif "km/h" in spd_unit_hint or "kph" in spd_unit_hint or "kmh" in spd_unit_hint:
                unit = "km/h"
            elif "m/s" in spd_unit_hint:
                unit = "m/s"
            else:
                unit = "km/h" if statistics.median(raw_spds) > 75.0 else "m/s"
        scale = 0.44704 if unit == "mph" else (1.0 / 3.6 if unit == "km/h" else 1.0)
        speeds_ms = [float(r["spd"] or 0.0) * scale for r in chosen_rows]

    steers_rad: Optional[List[float]] = None
    if any(r["steer"] is not None for r in chosen_rows):
        is_deg = "rad" not in steer_unit_hint
        steers_rad = [
            (math.radians(float(r["steer"])) if is_deg else float(r["steer"]))
            if r["steer"] is not None
            else 0.0
            for r in chosen_rows
        ]

    return _build_geodetic_point_set(
        times_s=[r["t"] for r in chosen_rows],
        latitudes=[r["lat"] for r in chosen_rows],
        longitudes=[r["lon"] for r in chosen_rows],
        altitudes=[float(r["alt"] or 0.0) for r in chosen_rows],
        speeds_ms=speeds_ms,
        steers_rad=steers_rad,
        throttles=[float(r["thr"] or 0.0) for r in chosen_rows],
        brakes=[float(r["brk"] or 0.0) for r in chosen_rows],
        gears=[r["gear"] for r in chosen_rows] if any(r["gear"] is not None for r in chosen_rows) else None,
        rpms=[r["rpm"] for r in chosen_rows] if any(r["rpm"] is not None for r in chosen_rows) else None,
        selected_lap=selected_lap,
        selection_source=selection_source,
        metadata={},
    )


def _extract_rcz_points(
    rcz_path: Path,
    *,
    target_lap: Optional[int] = None,
    lap_selection: str = "fastest",
    min_lap_seconds: float = 0.0,
    max_lap_seconds: float = float("inf"),
) -> Dict[str, Any]:
    with zipfile.ZipFile(rcz_path, "r") as zf:
        names = zf.namelist()
        # 1) Embedded CSV export inside .rcz
        csv_members = [n for n in names if n.lower().endswith(".csv")]
        if csv_members:
            text = zf.read(csv_members[0]).decode("utf-8", errors="replace")
            return _extract_csv_points_from_text(
                text,
                target_lap=target_lap,
                lap_selection=lap_selection,
                min_lap_seconds=min_lap_seconds,
                max_lap_seconds=max_lap_seconds,
            )

        # 2) RaceChrono JSON format (session.json + route/samples)
        json_members = [n for n in names if n.lower().endswith(".json")]
        for member in json_members:
            try:
                doc = json.loads(zf.read(member).decode("utf-8", errors="replace"))
            except Exception:
                continue
            if isinstance(doc, dict) and isinstance(doc.get("samples"), list) and doc["samples"]:
                samples = doc["samples"]
                if "latitude" in samples[0] and "longitude" in samples[0]:
                    return _build_geodetic_point_set(
                        times_s=[float(s.get("timeS", s.get("time", i * 0.1))) for i, s in enumerate(samples)],
                        latitudes=[float(s["latitude"]) for s in samples],
                        longitudes=[float(s["longitude"]) for s in samples],
                        altitudes=[float(s.get("altitude", 0.0)) for s in samples],
                        speeds_ms=[float(s.get("speedMS", s.get("speed", 0.0))) for s in samples],
                        selected_lap=target_lap or 1,
                        selection_source="rcz-json",
                        metadata={
                            "trackName": str(doc.get("trackName") or ""),
                            "carName": str(doc.get("carName") or ""),
                        },
                    )

    raise ValueError(
        f"Unsupported .rcz archive layout in {rcz_path.name}: no embedded CSV or JSON GPS samples found. "
        "Export CSV or MoTeC .ld from RaceChrono and pass that file instead."
    )


def _extract_xrk_points(
    xrk_path: Path,
    *,
    target_lap: Optional[int] = None,
    lap_selection: str = "fastest",
) -> Dict[str, Any]:
    try:
        import libxrk  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ValueError(
            f"Reading AiM {xrk_path.suffix} files requires optional package 'libxrk' "
            "(pip install libxrk), or export MoTeC .ld / CSV from Race Studio 3."
        ) from exc

    log = libxrk.aim_xrk(str(xrk_path))
    channels = getattr(log, "channels", {}) or {}

    def _find_ch(*aliases: str):
        lower_map = {k.lower(): v for k, v in channels.items()}
        for a in aliases:
            if a.lower() in lower_map:
                return lower_map[a.lower()]
        for a in aliases:
            al = a.lower()
            for k, v in lower_map.items():
                if al in k:
                    return v
        return None

    lat_ch = _find_ch("GPS Latitude", "GPS_Latitude", "Latitude")
    lon_ch = _find_ch("GPS Longitude", "GPS_Longitude", "Longitude")
    if lat_ch is None or lon_ch is None:
        raise ValueError("XRK file does not contain GPS Latitude/Longitude channels")

    alt_ch = _find_ch("GPS Altitude", "GPS_Altitude", "Altitude")
    spd_ch = _find_ch("GPS Speed", "GPS_Speed", "Speed")
    times_ms = np.asarray(lat_ch.column("timecodes").to_numpy(), dtype=np.float64)
    times_s = times_ms / 1000.0
    lat = np.asarray(lat_ch.column(lat_ch.column_names[1]).to_numpy(), dtype=np.float64)
    lon = np.asarray(lon_ch.column(lon_ch.column_names[1]).to_numpy(), dtype=np.float64)
    alt = (
        np.interp(
            times_s,
            np.asarray(alt_ch.column("timecodes").to_numpy(), dtype=np.float64) / 1000.0,
            np.asarray(alt_ch.column(alt_ch.column_names[1]).to_numpy(), dtype=np.float64),
        )
        if alt_ch is not None
        else None
    )
    spd = (
        np.interp(
            times_s,
            np.asarray(spd_ch.column("timecodes").to_numpy(), dtype=np.float64) / 1000.0,
            np.asarray(spd_ch.column(spd_ch.column_names[1]).to_numpy(), dtype=np.float64),
        )
        if spd_ch is not None
        else None
    )

    laps_tbl = getattr(log, "laps", None)
    selected_lap = 1
    s_idx, e_idx = 0, len(times_s)
    if laps_tbl is not None and len(laps_tbl) > 0 and lap_selection != "all":
        starts = np.asarray(laps_tbl.column("start_time").to_numpy(), dtype=np.float64) / 1000.0
        ends = np.asarray(laps_tbl.column("end_time").to_numpy(), dtype=np.float64) / 1000.0
        cands = []
        for idx_l, (st, en) in enumerate(zip(starts, ends), start=1):
            dur = float(en - st)
            if dur > 10.0:
                cands.append((dur, idx_l, st, en))
        if cands:
            if target_lap is not None:
                chosen = next((c for c in cands if c[1] == target_lap), None)
                if chosen is None:
                    raise ValueError(f"Requested XRK lap {target_lap} not found")
            elif lap_selection == "first":
                chosen = cands[0]
            else:
                chosen = min(cands, key=lambda c: c[0])
            selected_lap = chosen[1]
            s_idx = int(np.searchsorted(times_s, chosen[2], side="left"))
            e_idx = int(np.searchsorted(times_s, chosen[3], side="right"))

    return _build_geodetic_point_set(
        times_s=times_s[s_idx:e_idx],
        latitudes=lat[s_idx:e_idx],
        longitudes=lon[s_idx:e_idx],
        altitudes=alt[s_idx:e_idx] if alt is not None else None,
        speeds_ms=spd[s_idx:e_idx] if spd is not None else None,
        selected_lap=selected_lap,
        selection_source="xrk",
        metadata=dict(getattr(log, "metadata", {}) or {}),
    )


def extract_telemetry_points(
    path: str | Path,
    *,
    target_lap: Optional[int] = None,
    lap_selection: str = "fastest",
    channel_overrides: Optional[Mapping[str, str]] = None,
    parser_path: Optional[str] = None,
    min_lap_seconds: float = 0.0,
    max_lap_seconds: float = float("inf"),
    speed_unit: str = "auto",
) -> Dict[str, Any]:
    """Load GPS telemetry from ``.ld``, ``.ibt``, ``.csv``, ``.rcz``, or ``.xrk``."""
    src = Path(path).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"Telemetry file not found: {src}")
    ext = src.suffix.lower()
    if ext == ".ld":
        return motec.extract_motec_points(
            src,
            parser_path=parser_path,
            channel_overrides=dict(channel_overrides) if channel_overrides else None,
            target_lap=target_lap,
            lap_selection=lap_selection,
            min_lap_seconds=min_lap_seconds,
            max_lap_seconds=max_lap_seconds,
            speed_unit=speed_unit,
        )
    if ext == ".ibt":
        return _extract_ibt_points(
            src,
            target_lap=target_lap,
            lap_selection=lap_selection,
            min_lap_seconds=max(min_lap_seconds, 10.0),
            max_lap_seconds=max_lap_seconds if math.isfinite(max_lap_seconds) else 600.0,
        )
    if ext == ".csv":
        return _extract_csv_points_from_text(
            src.read_text(encoding="utf-8-sig", errors="replace"),
            target_lap=target_lap,
            lap_selection=lap_selection,
            min_lap_seconds=min_lap_seconds,
            max_lap_seconds=max_lap_seconds,
            speed_unit=speed_unit,
        )
    if ext == ".rcz":
        return _extract_rcz_points(
            src,
            target_lap=target_lap,
            lap_selection=lap_selection,
            min_lap_seconds=min_lap_seconds,
            max_lap_seconds=max_lap_seconds,
        )
    if ext in (".xrk", ".xrz"):
        return _extract_xrk_points(
            src,
            target_lap=target_lap,
            lap_selection=lap_selection,
        )
    raise ValueError(
        f"Unsupported telemetry extension {ext!r}. Supported: {', '.join(SUPPORTED_TELEMETRY_EXTENSIONS)}"
    )
