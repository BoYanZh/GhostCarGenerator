"""Tests for multi-format telemetry loading, AC fast_lane.ai auto-calibration, and AC deploy."""
from __future__ import annotations

import json
import math
import struct
import zipfile
from pathlib import Path

import numpy as np
import pytest

from ghost_car_generator.ac_track import (
    auto_calibrate_from_ac_track,
    discover_ac_track_files,
    read_ac_fast_lane,
)
from ghost_car_generator.runtime_ghost import (
    deploy_runtime_ghost_to_ac,
    export_runtime_ghost_from_telemetry,
)
from ghost_car_generator.telemetry import extract_telemetry_points


def _write_synthetic_fast_lane(ai_path: Path, n: int = 240, radius_m: float = 200.0) -> np.ndarray:
    """Create a closed oval AC fast_lane.ai in XZ plane (with slight elevation)."""
    ai_path.parent.mkdir(parents=True, exist_ok=True)
    angles = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    xs = radius_m * np.cos(angles)
    zs = 0.65 * radius_m * np.sin(angles)
    ys = 2.0 * np.sin(angles)
    pts = np.column_stack((xs, ys, zs))
    dists = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))]

    buf = bytearray(struct.pack("<4i", 7, n, 0, 0))
    for i in range(n):
        buf.extend(struct.pack("<4fi", float(xs[i]), float(ys[i]), float(zs[i]), float(dists[i]), i))
    ai_path.write_bytes(bytes(buf))
    return pts


def _make_gps_oval(
    n: int = 200,
    radius_m: float = 200.0,
    duration_s: float = 60.0,
    lat0: float = 39.538,
    lon0: float = -122.331,
):
    earth_r = 6371008.8
    cos_lat = math.cos(math.radians(lat0))
    angles = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    east = radius_m * np.cos(angles)
    north = 0.65 * radius_m * np.sin(angles)
    alt = 50.0 + 2.0 * np.sin(angles)
    lats = lat0 + np.degrees(north / earth_r)
    lons = lon0 + np.degrees(east / (earth_r * cos_lat))
    times = np.linspace(0.0, duration_s, n)
    return times, lats, lons, alt


def test_csv_multi_lap_fastest_and_units(tmp_path: Path):
    t1, lat1, lon1, alt1 = _make_gps_oval(n=120, duration_s=65.0)
    t2, lat2, lon2, alt2 = _make_gps_oval(n=120, duration_s=58.0)

    lines = [
        "Format,RaceChrono v7",
        "Session,Test Track",
        "Time,Lap #,Latitude,Longitude,Altitude,Speed,Steering Angle",
        "s,,deg,deg,m,km/h,deg",
    ]
    for i in range(len(t1)):
        lines.append(f"{t1[i]:.3f},1,{lat1[i]:.7f},{lon1[i]:.7f},{alt1[i]:.2f},108.0,5.0")
    for i in range(len(t2)):
        lines.append(f"{65.0 + t2[i]:.3f},2,{lat2[i]:.7f},{lon2[i]:.7f},{alt2[i]:.2f},120.0,-4.0")

    csv_path = tmp_path / "session.csv"
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    pts = extract_telemetry_points(csv_path)
    assert pts["selectedLap"] == 2
    assert abs(pts["lapTimeS"] - 58.0) < 0.2
    assert pts["coordinateSystem"] == "geodetic"
    assert abs(pts["points"][0]["speedMS"] - (120.0 / 3.6)) < 0.1
    assert abs(pts["points"][0]["steerRad"] - math.radians(-4.0)) < 1e-4


def test_rcz_embedded_csv_extraction(tmp_path: Path):
    t, lat, lon, alt = _make_gps_oval(n=80, duration_s=45.0)
    csv_lines = ["Time,Latitude,Longitude,Altitude,Speed (m/s)"]
    for i in range(len(t)):
        csv_lines.append(f"{t[i]:.3f},{lat[i]:.7f},{lon[i]:.7f},{alt[i]:.2f},30.0")
    rcz_path = tmp_path / "lap.rcz"
    with zipfile.ZipFile(rcz_path, "w") as zf:
        zf.writestr("export.csv", "\n".join(csv_lines))

    pts = extract_telemetry_points(rcz_path)
    assert len(pts["points"]) == 80
    assert abs(pts["lapTimeS"] - 45.0) < 0.1


def test_ibt_telemetry_extraction(tmp_path: Path):
    # Build a minimal valid iRacing .ibt with 2 complete laps (lap 1 = 50s, lap 2 = 42s)
    n1, n2 = 100, 85
    t1, lat1, lon1, alt1 = _make_gps_oval(n=n1, duration_s=50.0)
    t2, lat2, lon2, alt2 = _make_gps_oval(n=n2, duration_s=42.0)

    # Prefix 2 samples at 0.99 to trigger wrap into lap 1, then lap 1, then lap 2, then 2 wrap samples
    times = np.concatenate([[-0.5], t1, 50.0 + t2, [92.5]])
    pcts = np.concatenate([[0.99], np.linspace(0.01, 0.99, n1), np.linspace(0.01, 0.99, n2), [0.01]])
    lats = np.concatenate([[lat1[-1]], lat1, lat2, [lat2[0]]])
    lons = np.concatenate([[lon1[-1]], lon1, lon2, [lon2[0]]])
    alts = np.concatenate([[alt1[-1]], alt1, alt2, [alt2[0]]])
    spds = np.full(len(times), 32.0, dtype=np.float64)

    var_defs = [
        ("SessionTime", 5, 0),
        ("LapDistPct", 5, 8),
        ("Lat", 5, 16),
        ("Lon", 5, 24),
        ("Alt", 5, 32),
        ("Speed", 5, 40),
    ]
    buf_len = 48
    session_bytes = b"TrackName: Synthetic Oval\nCarPath: gr86\n"
    session_offset = 112
    var_offset = session_offset + len(session_bytes)
    data_offset = var_offset + len(var_defs) * 144

    header = bytearray(112)
    struct.pack_into(
        "<12i",
        header,
        0,
        2,
        1,
        60,
        1,
        len(session_bytes),
        session_offset,
        len(var_defs),
        var_offset,
        1,
        buf_len,
        0,
        0,
    )
    struct.pack_into("<2i", header, 48, len(times), data_offset)

    var_table = bytearray()
    for name, vtype, voff in var_defs:
        entry = bytearray(144)
        struct.pack_into("<3i", entry, 0, vtype, voff, 1)
        nb = name.encode("latin-1")
        entry[16 : 16 + len(nb)] = nb
        var_table.extend(entry)

    samples = bytearray()
    for i in range(len(times)):
        samples.extend(struct.pack("<6d", times[i], pcts[i], lats[i], lons[i], alts[i], spds[i]))

    ibt_path = tmp_path / "test.ibt"
    ibt_path.write_bytes(bytes(header) + session_bytes + bytes(var_table) + bytes(samples))

    pts = extract_telemetry_points(ibt_path)
    assert pts["selectedLap"] == 2
    assert 41.0 <= pts["lapTimeS"] <= 43.0
    assert pts["metadata"]["trackName"] == "Synthetic Oval"


def test_xrk_missing_libxrk_gives_clear_error(tmp_path: Path):
    xrk_path = tmp_path / "sample.xrk"
    xrk_path.write_bytes(b"XRK_DUMMY")
    with pytest.raises(ValueError, match="libxrk"):
        extract_telemetry_points(xrk_path)


def test_fast_lane_auto_calibration_and_deploy(tmp_path: Path):
    # Create synthetic AC track directory with layout + fast_lane.ai
    ac_root = tmp_path / "assettocorsa"
    track_dir = ac_root / "content" / "tracks" / "my_custom_track"
    ai_path = track_dir / "gp_layout" / "ai" / "fast_lane.ai"
    _write_synthetic_fast_lane(ai_path, n=240, radius_m=200.0)

    loaded_ai = read_ac_fast_lane(ai_path)
    assert loaded_ai.shape == (240, 3)

    discovered = discover_ac_track_files(track_dir, layout="gp_layout")
    assert discovered["trackName"] == "my_custom_track"
    assert discovered["layout"] == "gp_layout"
    assert discovered["fastLanePath"] == ai_path

    # Create matching CSV telemetry
    t, lat, lon, alt = _make_gps_oval(n=200, radius_m=200.0, duration_s=62.5)
    csv_lines = ["Time,Latitude,Longitude,Altitude,Speed"]
    for i in range(len(t)):
        csv_lines.append(f"{t[i]:.3f},{lat[i]:.7f},{lon[i]:.7f},{alt[i]:.2f},28.0")
    csv_path = tmp_path / "my_lap.csv"
    csv_path.write_text("\n".join(csv_lines) + "\n", encoding="utf-8")

    pts = extract_telemetry_points(csv_path)
    track_ref, road_surf, full_surf = auto_calibrate_from_ac_track(
        track_dir, pts, layout="gp_layout", load_kn5_surfaces=False
    )
    assert road_surf is None and full_surf is None
    assert track_ref["track"] == {"name": "my_custom_track", "layout": "gp_layout"}
    assert track_ref["calibration"]["orderedRmseM"] < 2.0

    # End-to-end export directly from CSV + ac_track_dir (zero .acreplay!)
    out_json = tmp_path / "out.json"
    res = export_runtime_ghost_from_telemetry(
        csv_path,
        out_json,
        ac_track_dir=track_dir,
        ac_layout="gp_layout",
        car_id="ks_mazda_mx5_cup",
    )
    assert res["autoCalibrated"] is True
    assert res["trackId"] == "my_custom_track/gp_layout"
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["schema"] == "ghost-runtime-v1"
    assert payload["trackId"] == "my_custom_track/gp_layout"
    assert payload["carId"] == "ks_mazda_mx5_cup"
    assert len(payload["frames"]) > 100

    # Deploy into AC tracks/<track>/<layout>/
    (ac_root / "apps" / "lua" / "ghostcar_hotlap").mkdir(parents=True, exist_ok=True)
    dep = deploy_runtime_ghost_to_ac(
        out_json,
        res["trackId"],
        ac_root=ac_root,
        ghost_name="my_lap_1m02",
        sync_lua=True,
    )
    expected = ac_root / "apps" / "lua" / "ghostcar_hotlap" / "tracks" / "my_custom_track" / "gp_layout" / "my_lap_1m02.json"
    assert Path(dep["deployedPath"]) == expected
    assert expected.is_file()
    assert (ac_root / "apps" / "lua" / "ghostcar_hotlap" / "ghostcar_hotlap.lua").is_file()
