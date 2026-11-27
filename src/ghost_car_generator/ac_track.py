"""Assetto Corsa track packages and GPS-to-world calibration."""
from __future__ import annotations

__all__ = [
    "auto_calibrate_from_ac_track",
    "calibrate_track",
    "discover_ac_track_files",
    "load_track_package",
    "load_track_reference_path",
    "read_ac_fast_lane",
]

import configparser
import json
import math
import struct
from pathlib import Path, PurePosixPath

import numpy as np

from .acreplay import parse_acreplay
from .motec import extract_motec_points
from .telemetry import extract_telemetry_points

EARTH_RADIUS_M = 6371008.8
BUILTIN_TRACK_ROOT = Path(__file__).resolve().parent / "resources" / "tracks"


def _read_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _resolve_track_path(path):
    value = str(path)
    if not value.startswith("builtin:"):
        return Path(path).expanduser()
    relative = value[len("builtin:") :].replace("\\", "/")
    parts = PurePosixPath(relative).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Invalid built-in track resource: {}".format(value))
    root = BUILTIN_TRACK_ROOT.resolve()
    resolved = root.joinpath(*parts).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError("Built-in track resource escapes package root: {}".format(value))
    return resolved


def load_track_package(path):
    """Load a track JSON or package directory and resolve packaged surfaces."""
    path = _resolve_track_path(path)
    surfaces = []
    if path.is_dir():
        manifest_path = next(
            (
                candidate
                for candidate in (path / "manifest.json", path / "package.json")
                if candidate.is_file()
            ),
            None,
        )
        if manifest_path is None:
            raise ValueError("Track package is missing manifest.json/package.json: {}".format(path))
        manifest = _read_json(manifest_path)
        default_reference = (
            "calibration.json" if manifest_path.name == "manifest.json" else "track.json"
        )
        reference_path = path / manifest.get("trackReference", default_reference)
        surfaces = [path / item for item in manifest.get("surfaces", [])]
    else:
        manifest = None
        reference_path = path
    if not reference_path.is_file():
        raise ValueError("Track reference does not exist: {}".format(reference_path))
    reference = _read_json(reference_path)
    return reference, reference_path, surfaces, manifest


def _path_length(points, closed=False):
    points = np.asarray(points, dtype=np.float64)
    if len(points) < 2:
        return 0.0
    delta = np.diff(points, axis=0)
    length = float(np.linalg.norm(delta, axis=1).sum())
    if closed:
        length += float(np.linalg.norm(points[0] - points[-1]))
    return length


def _resample_path(points, count, closed=True):
    points = np.asarray(points, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] not in (2, 3):
        raise ValueError("Calibration paths must be Nx2 or Nx3")
    if len(points) < 3:
        raise ValueError("Calibration paths require at least three points")
    keep = np.r_[True, np.linalg.norm(np.diff(points, axis=0), axis=1) > 1e-6]
    points = points[keep]
    if len(points) < 3:
        raise ValueError("Calibration path has fewer than three distinct points")
    if closed and np.linalg.norm(points[-1] - points[0]) > 1e-6:
        points = np.vstack((points, points[0]))
    distance = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
    if distance[-1] <= 0.0:
        raise ValueError("Calibration path length must be positive")
    targets = np.linspace(0.0, distance[-1], int(count), endpoint=not closed)
    return np.column_stack(
        [np.interp(targets, distance, points[:, column]) for column in range(points.shape[1])]
    )


def _similarity_fit(source, target, allow_scale):
    source = np.asarray(source, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    source_mean = source.mean(axis=0)
    target_mean = target.mean(axis=0)
    source_centered = source - source_mean
    target_centered = target - target_mean
    covariance = target_centered.T @ source_centered
    u, singular, vt = np.linalg.svd(covariance)
    denominator = float(np.sum(source_centered ** 2))
    candidates = []
    for parity in (1.0, -1.0):
        correction = np.diag([1.0, parity])
        rotation = u @ correction @ vt
        numerator = float(np.sum(singular * np.diag(correction)))
        scale = numerator / denominator if allow_scale and denominator > 0.0 else 1.0
        if scale <= 0.0:
            continue
        translation = target_mean - scale * source_mean @ rotation.T
        fitted = scale * source @ rotation.T + translation
        rms = float(np.sqrt(np.mean(np.sum((fitted - target) ** 2, axis=1))))
        candidates.append((rms, rotation, translation, scale))
    if not candidates:
        raise ValueError("Unable to fit a positive GPS-to-AC scale")
    return min(candidates, key=lambda item: item[0])


def _select_replay_reference(replay, car_index, reference_lap):
    if car_index < 0 or car_index >= len(replay["cars"]):
        raise ValueError("Reference car index {} is out of range".format(car_index))
    frames = replay["cars"][car_index]["frames"]
    groups = {}
    for frame in frames:
        groups.setdefault(int(frame["currentLap"]), []).append(frame["positionM"])
    candidates = []
    for lap_number, positions in groups.items():
        path = np.asarray(positions, dtype=np.float64)
        if len(path) < 3:
            continue
        length = _path_length(path)
        closure = float(np.linalg.norm(path[-1] - path[0]))
        candidates.append((lap_number, path, length, closure))
    if not candidates:
        raise ValueError("Reference replay contains no usable car path")
    if reference_lap is not None:
        selected = next((item for item in candidates if item[0] == reference_lap), None)
        if selected is None:
            raise ValueError("Reference replay does not contain lap {}".format(reference_lap))
    else:
        closed = [item for item in candidates if item[3] <= max(10.0, item[2] * 0.02)]
        selected = max(closed or candidates, key=lambda item: item[2])
    return selected


_NON_ROAD_KN5_HINTS = (
    "tree",
    "flag",
    "tower",
    "crowd",
    "spectator",
    "sky",
    "camera",
    "marshal",
    "balloon",
    "heli",
    "plane",
    "sign",
    "billboard",
    "light",
    "tent",
    "building",
    "garage",
    "pitbox",
)


def read_ac_fast_lane(ai_path):
    """Read an Assetto Corsa ``fast_lane.ai`` binary file into an Nx3 AC-world array."""
    path = Path(ai_path).expanduser()
    data = path.read_bytes()
    if len(data) < 16:
        raise ValueError("fast_lane.ai file is too small: {}".format(path))
    _version, detail_count, _lap_time, _sample_count = struct.unpack_from("<4i", data, 0)
    if detail_count < 3:
        raise ValueError("fast_lane.ai has fewer than 3 detail points: {}".format(path))
    needed = 16 + detail_count * 20
    if len(data) < needed:
        raise ValueError("fast_lane.ai is truncated: {}".format(path))
    raw = np.frombuffer(
        data,
        dtype=np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("dist", "<f4"), ("id", "<i4")]),
        count=detail_count,
        offset=16,
    )
    points = np.column_stack((raw["x"], raw["y"], raw["z"])).astype(np.float64)
    return points


def discover_ac_track_files(track_dir, layout=None):
    """Discover ``fast_lane.ai`` and road-bearing ``.kn5`` files in an AC track directory."""
    root = Path(track_dir).expanduser()
    if not root.is_dir():
        raise ValueError("AC track directory does not exist: {}".format(root))
    track_name = root.name
    layout_name = str(layout or "").strip()

    # Locate fast_lane.ai
    ai_candidates = []
    if layout_name:
        ai_candidates.append(root / layout_name / "ai" / "fast_lane.ai")
    ai_candidates.append(root / "ai" / "fast_lane.ai")
    fast_lane = next((p for p in ai_candidates if p.is_file()), None)
    if fast_lane is None and not layout_name:
        # Auto-pick single layout if only one subdir has ai/fast_lane.ai
        found = sorted(root.glob("*/ai/fast_lane.ai"))
        if len(found) == 1:
            fast_lane = found[0]
            layout_name = found[0].parent.parent.name
    if fast_lane is None:
        raise ValueError(
            "Could not find ai/fast_lane.ai in {} (layout={!r})".format(root, layout_name)
        )

    # Locate KN5 files via models_<layout>.ini or models.ini
    ini_candidates = []
    if layout_name:
        ini_candidates.append(root / f"models_{layout_name}.ini")
    ini_candidates.append(root / "models.ini")
    models_ini = next((p for p in ini_candidates if p.is_file()), None)

    kn5_files = []
    if models_ini is not None:
        parser = configparser.ConfigParser(strict=False)
        try:
            parser.read(models_ini, encoding="utf-8", errors="ignore")
        except TypeError:
            parser.read(models_ini, encoding="utf-8")
        for section in parser.sections():
            if not section.upper().startswith("MODEL_"):
                continue
            fname = parser.get(section, "FILE", fallback="").strip()
            if not fname:
                continue
            cand = root / fname
            if cand.is_file() and cand not in kn5_files:
                kn5_files.append(cand)
    else:
        for name in (f"{layout_name}.kn5" if layout_name else "", f"{track_name}.kn5"):
            if name and (root / name).is_file():
                kn5_files.append(root / name)
        if not kn5_files:
            kn5_files = sorted(root.glob("*.kn5"))

    # Prioritize road-bearing KN5 files (skip obvious scenery props when others exist)
    road_kn5 = [
        p
        for p in kn5_files
        if not any(hint in p.stem.lower() for hint in _NON_ROAD_KN5_HINTS)
    ]
    return {
        "trackName": track_name,
        "layout": layout_name,
        "fastLanePath": fast_lane,
        "kn5Paths": road_kn5 or kn5_files,
        "allKn5Paths": kn5_files,
    }


def load_track_reference_path(reference_path, car_index=0, reference_lap=None, layout=None):
    """Load the AC-world reference path from a package/JSON, AC track dir, ``.ai``, or replay."""
    path = Path(reference_path).expanduser()
    if path.suffix.casefold() == ".ai":
        points = read_ac_fast_lane(path)
        track_layout = ""
        track_name = path.parent.parent.name
        if path.parent.name.casefold() == "ai":
            parent_dir = path.parent.parent
            if (parent_dir.parent / "ui").is_dir() or (
                parent_dir.parent / f"models_{parent_dir.name}.ini"
            ).is_file():
                track_layout = parent_dir.name
                track_name = parent_dir.parent.name
        return {
            "path": points,
            "track": {"name": track_name, "layout": layout or track_layout},
            "sourcePath": path,
            "referenceLap": None,
            "referenceLengthM": _path_length(points, closed=True),
            "closureGapM": float(np.linalg.norm(points[-1] - points[0])),
        }

    if path.is_dir():
        has_manifest = (path / "manifest.json").is_file() or (path / "package.json").is_file()
        if not has_manifest:
            discovered = discover_ac_track_files(path, layout=layout)
            points = read_ac_fast_lane(discovered["fastLanePath"])
            return {
                "path": points,
                "track": {
                    "name": discovered["trackName"],
                    "layout": discovered["layout"],
                },
                "sourcePath": discovered["fastLanePath"],
                "referenceLap": None,
                "referenceLengthM": _path_length(points, closed=True),
                "closureGapM": float(np.linalg.norm(points[-1] - points[0])),
                "kn5Paths": discovered["kn5Paths"],
            }

    if path.is_dir() or path.suffix.casefold() == ".json":
        reference, source_path, _, _ = load_track_package(path)
        points = np.asarray(reference.get("referencePathAc"), dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 3 or len(points) < 3:
            raise ValueError("Track reference needs an Nx3 referencePathAc")
        return {
            "path": points,
            "track": dict(reference.get("track", {})),
            "sourcePath": source_path,
            "referenceLap": None,
            "closureGapM": float(np.linalg.norm(points[-1] - points[0])),
        }

    replay = parse_acreplay(path, max_frames=0)
    lap_number, points, length, closure = _select_replay_reference(
        replay, car_index, reference_lap
    )
    return {
        "path": points,
        "track": {
            "name": replay["header"]["track"],
            "layout": replay["header"]["trackConfig"],
        },
        "sourcePath": path,
        "referenceLap": int(lap_number),
        "referenceLengthM": float(length),
        "closureGapM": float(closure),
    }


def _common_enu(point_sets):
    if any(item.get("coordinateSystem") != "geodetic" for item in point_sets):
        raise ValueError("calibrate-track requires latitude/longitude MoTeC data")
    origins = [item["origin"] for item in point_sets]
    origin = {
        "latitudeDeg": float(np.median([item["latitudeDeg"] for item in origins])),
        "longitudeDeg": float(np.median([item["longitudeDeg"] for item in origins])),
        "altitudeM": float(np.median([item["altitudeM"] for item in origins])),
    }
    lat0 = math.radians(origin["latitudeDeg"])
    paths = []
    for item in point_sets:
        item_origin = item["origin"]
        east = (
            math.radians(item_origin["longitudeDeg"] - origin["longitudeDeg"])
            * EARTH_RADIUS_M
            * math.cos(lat0)
        )
        north = (
            math.radians(item_origin["latitudeDeg"] - origin["latitudeDeg"])
            * EARTH_RADIUS_M
        )
        up = item_origin["altitudeM"] - origin["altitudeM"]
        paths.append(
            np.asarray(
                [
                    [point["xM"] + east, point["yM"] + north, point["zM"] + up]
                    for point in item["points"]
                ],
                dtype=np.float64,
            )
        )
    return origin, paths


def _best_ordered_fit(source, target, allow_scale):
    best = None
    for reversed_path in (False, True):
        candidate = target[::-1] if reversed_path else target
        for shift in range(len(candidate)):
            ordered = np.roll(candidate, shift, axis=0)
            rms, rotation, translation, scale = _similarity_fit(
                source, ordered, allow_scale
            )
            result = (
                rms,
                rotation,
                translation,
                scale,
                shift,
                reversed_path,
                ordered,
            )
            if best is None or result[0] < best[0]:
                best = result
    return best


def calibrate_track(
    reference_path,
    ld_paths,
    laps=None,
    reference_car=0,
    reference_lap=None,
    track_name=None,
    layout=None,
    alignment_samples=500,
    allow_scale=False,
    max_scale_error=0.03,
    max_rmse_m=8.0,
    parser_path=None,
    channel_overrides=None,
):
    """Fit one shared GPS-to-AC transform from one or more complete LD laps."""
    ld_paths = [Path(item).expanduser() for item in ld_paths]
    if not ld_paths:
        raise ValueError("calibrate-track requires at least one MoTeC LD file")
    alignment_samples = int(alignment_samples)
    if alignment_samples < 50:
        raise ValueError("Alignment sample count must be at least 50")
    lap_values = list(laps or [])
    if len(lap_values) == 1 and len(ld_paths) > 1:
        lap_values *= len(ld_paths)
    if lap_values and len(lap_values) != len(ld_paths):
        raise ValueError("Provide zero, one, or one --lap value per LD file")

    point_sets = []
    for index, path in enumerate(ld_paths):
        loader = (
            extract_motec_points
            if path.suffix.lower() == ".ld"
            else extract_telemetry_points
        )
        point_sets.append(
            loader(
                path,
                parser_path=parser_path,
                channel_overrides=channel_overrides,
                target_lap=lap_values[index] if lap_values else None,
            )
        )
    origin, source_paths = _common_enu(point_sets)
    source_resampled = np.asarray(
        [_resample_path(path, alignment_samples) for path in source_paths]
    )
    median_source = np.median(source_resampled, axis=0)

    reference = load_track_reference_path(
        reference_path,
        car_index=reference_car,
        reference_lap=reference_lap,
        layout=layout,
    )
    reference_resampled = _resample_path(reference["path"], alignment_samples)
    fit = _best_ordered_fit(
        median_source[:, :2], reference_resampled[:, [0, 2]], allow_scale
    )
    rms, rotation, translation, scale, shift, reversed_path, _ = fit
    if abs(scale - 1.0) > float(max_scale_error):
        raise ValueError(
            "Calibration scale {:.6f} exceeds allowed error {:.2%}".format(
                scale, max_scale_error
            )
        )
    if rms > float(max_rmse_m):
        raise ValueError(
            "Calibration ordered RMSE {:.3f}m exceeds limit {:.3f}m".format(
                rms, max_rmse_m
            )
        )

    ordered_reference = reference_resampled[::-1] if reversed_path else reference_resampled
    ordered_reference = np.roll(ordered_reference, shift, axis=0)
    vertical_translation = float(
        np.median(ordered_reference[:, 1] - median_source[:, 2])
    )
    matrix = np.array(
        [
            [scale * rotation[0, 0], scale * rotation[0, 1], 0.0, translation[0]],
            [0.0, 0.0, 1.0, vertical_translation],
            [scale * rotation[1, 0], scale * rotation[1, 1], 0.0, translation[1]],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )

    all_nearest = []
    source_diagnostics = []
    target_horizontal = ordered_reference[:, [0, 2]]
    for path, resampled, point_set in zip(
        source_paths, source_resampled, point_sets
    ):
        transformed = scale * resampled[:, :2] @ rotation.T + translation
        distances = np.linalg.norm(
            transformed[:, None, :] - target_horizontal[None, :, :], axis=2
        ).min(axis=1)
        all_nearest.extend(distances.tolist())
        source_diagnostics.append(
            {
                "pointCount": len(point_set["points"]),
                "pathLengthM": _path_length(path[:, :2], closed=True),
                "nearestRmseM": float(np.sqrt(np.mean(distances ** 2))),
                "nearestP95M": float(np.percentile(distances, 95)),
                "nearestMaxM": float(np.max(distances)),
            }
        )
    nearest = np.asarray(all_nearest, dtype=np.float64)
    track = dict(reference["track"])
    if track_name is not None:
        track["name"] = track_name
    if layout is not None:
        track["layout"] = layout
    if not track.get("name"):
        raise ValueError("Track name is missing; pass --track-name")
    track.setdefault("layout", "")

    return {
        "schemaVersion": 1,
        "track": track,
        "origin": origin,
        "enuToAc": {"matrix": matrix.tolist()},
        "startLineAc": ordered_reference[0].tolist(),
        "referencePathAc": ordered_reference.tolist(),
        "calibration": {
            "method": "multi-lap-trajectory-shape-fit",
            "alignmentSamples": alignment_samples,
            "sourceCount": len(point_sets),
            "phaseShiftFraction": float(shift) / alignment_samples,
            "referenceReversed": bool(reversed_path),
            "reflected": bool(np.linalg.det(rotation) < 0.0),
            "scale": float(scale),
            "orderedRmseM": float(rms),
            "nearestRmseM": float(np.sqrt(np.mean(nearest ** 2))),
            "nearestP95M": float(np.percentile(nearest, 95)),
            "nearestMaxM": float(np.max(nearest)),
            "sourceLengthMedianM": float(
                np.median([item["pathLengthM"] for item in source_diagnostics])
            ),
            "targetLengthM": _path_length(target_horizontal, closed=True),
            "referenceClosureGapM": reference["closureGapM"],
            "sources": source_diagnostics,
        },
    }


def _merge_kn5_surfaces(kn5_paths, mesh_pattern):
    from .kn5 import read_kn5_surface
    from .track_surface import TrackSurface

    vertices_list = []
    triangles_list = []
    offset = 0
    for kn5_path in kn5_paths:
        try:
            surface, _ = read_kn5_surface(kn5_path, mesh_pattern=mesh_pattern)
        except ValueError:
            continue
        vertices_list.append(surface.vertices)
        triangles_list.append(surface.triangles + offset)
        offset += len(surface.vertices)
    if not vertices_list:
        return None
    return TrackSurface(np.vstack(vertices_list), np.vstack(triangles_list))


def auto_calibrate_from_ac_track(
    ac_track_dir,
    point_set,
    *,
    layout=None,
    track_name=None,
    alignment_samples=500,
    allow_scale=False,
    max_scale_error=0.05,
    max_rmse_m=15.0,
    load_kn5_surfaces=True,
):
    """Auto-calibrate a single telemetry point-set directly against an AC track folder.

    Uses ``fast_lane.ai`` for 3D centerline & exact start/finish line, and
    extracts road/terrain ``TrackSurface`` directly from the track's ``.kn5``
    files—requiring no pre-recorded ``.acreplay`` or pre-baked track package.
    """
    discovered = discover_ac_track_files(ac_track_dir, layout=layout)
    fast_lane = read_ac_fast_lane(discovered["fastLanePath"])
    origin, source_paths = _common_enu([point_set])
    source_path = source_paths[0]
    source_resampled = _resample_path(source_path, int(alignment_samples))
    reference_resampled = _resample_path(fast_lane, int(alignment_samples))

    fit = _best_ordered_fit(
        source_resampled[:, :2], reference_resampled[:, [0, 2]], allow_scale
    )
    rms, rotation, translation, scale, shift, reversed_path, _ = fit
    if abs(scale - 1.0) > float(max_scale_error):
        raise ValueError(
            "Auto-calibration scale {:.6f} exceeds allowed error {:.2%}".format(
                scale, max_scale_error
            )
        )
    if rms > float(max_rmse_m):
        raise ValueError(
            "Auto-calibration ordered RMSE {:.3f}m exceeds limit {:.3f}m".format(
                rms, max_rmse_m
            )
        )

    ordered_reference = reference_resampled[::-1] if reversed_path else reference_resampled
    ordered_reference = np.roll(ordered_reference, shift, axis=0)
    vertical_translation = float(
        np.median(ordered_reference[:, 1] - source_resampled[:, 2])
    )
    matrix = np.array(
        [
            [scale * rotation[0, 0], scale * rotation[0, 1], 0.0, translation[0]],
            [0.0, 0.0, 1.0, vertical_translation],
            [scale * rotation[1, 0], scale * rotation[1, 1], 0.0, translation[1]],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )

    resolved_track = {
        "name": track_name or discovered["trackName"],
        "layout": discovered["layout"],
    }
    track_ref = {
        "schemaVersion": 1,
        "track": resolved_track,
        "origin": origin,
        "enuToAc": {"matrix": matrix.tolist()},
        # fast_lane[0] is AC's exact start/finish line position.
        "startLineAc": fast_lane[0].tolist(),
        "referencePathAc": fast_lane.tolist(),
        "calibration": {
            "method": "ac-fast-lane-auto-fit",
            "fastLanePath": str(discovered["fastLanePath"]),
            "alignmentSamples": int(alignment_samples),
            "scale": float(scale),
            "orderedRmseM": float(rms),
            "referenceReversed": bool(reversed_path),
            "reflected": bool(np.linalg.det(rotation) < 0.0),
        },
    }

    road_surface = None
    full_surface = None
    if load_kn5_surfaces and discovered["kn5Paths"]:
        road_surface = _merge_kn5_surfaces(
            discovered["kn5Paths"], r"^(1ROAD|1PIT|\d*ROAD|\d*ASPHALT)"
        )
        full_surface = _merge_kn5_surfaces(
            discovered["kn5Paths"],
            r"^(1ROAD|1PIT|1KERB|1CURB|1GRASS|1SAND|1GRAVEL|1TERRAIN|\d*ROAD|\d*KERB)",
        ) or road_surface
    return track_ref, road_surface, full_surface
