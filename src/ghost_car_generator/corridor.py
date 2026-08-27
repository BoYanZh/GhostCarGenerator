"""Corridor-constrained lateral-offset correction for iRacing BLAP/OLAP files."""
from __future__ import annotations

__all__ = ["constrain_blap"]

from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from .iracing import pack_blap, parse_blap

_DEFAULT_TOLERANCE_M = 1.0
_DEFAULT_SMOOTH_BINS = 6
_SEARCH_RANGE_M = 50.0
_SEARCH_STEP_M = 0.02


def _sample_offsets(data: Dict[str, Any]) -> np.ndarray:
    return np.array(
        [float(sample["lateralOffsetM"]) for sample in data["samples"]],
        dtype=float,
    )


def _moving_average_cyclic(values: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or len(values) <= window:
        return values
    ext = np.concatenate([values[-window:], values, values[:window]])
    kernel = np.ones(window) / window
    smoothed = np.convolve(ext, kernel, mode="same")
    return smoothed[window:-window][: len(values)]


def _validate_shapes(source, left, right) -> None:
    source_track = source["header"].get("trackName", "")
    for label, lap in (("left", left), ("right", right)):
        track = lap["header"].get("trackName", "")
        if track != source_track:
            raise ValueError(
                "{} lap track {!r} does not match source track {!r}".format(
                    label, track, source_track
                )
            )
    source_bins = [
        int(sector["numBins"]) for sector in source["summary"].get("sectors", [])
    ]
    if not source_bins:
        raise ValueError("Source lap has no sector records")
    for label, lap in (("left", left), ("right", right)):
        bins = [
            int(sector["numBins"]) for sector in lap["summary"].get("sectors", [])
        ]
        if bins != source_bins:
            raise ValueError(
                "{} lap sector grid does not match the source lap".format(label)
            )
    if len(source["samples"]) != sum(source_bins):
        raise ValueError("Source lap sample count does not match its sector grid")


def _optimal_translation(
    offsets: np.ndarray, corridor_lo: np.ndarray, corridor_hi: np.ndarray
) -> float:
    dlo = corridor_lo - offsets
    dhi = corridor_hi - offsets
    grid = np.arange(-_SEARCH_RANGE_M, _SEARCH_RANGE_M + _SEARCH_STEP_M, _SEARCH_STEP_M)
    below = np.maximum(0.0, dlo[None, :] - grid[:, None])
    above = np.maximum(0.0, grid[:, None] - dhi[None, :])
    worst = np.max(np.maximum(below, above), axis=1)
    shift = float(grid[int(np.argmin(worst))])
    best_worst = float(worst[int(np.argmin(worst))])
    for _ in range(3):
        candidates = []
        for delta in (shift - 0.001, shift, shift + 0.001):
            violation = np.max(
                np.maximum(
                    np.maximum(0.0, dlo - delta),
                    np.maximum(0.0, delta - dhi),
                )
            )
            candidates.append((violation, delta))
        violation, shift = min(candidates)
        best_worst = min(best_worst, violation)
    return shift


def constrain_blap(
    source_path,
    left_path,
    right_path,
    mode: str = "translate",
    tolerance_m: float = _DEFAULT_TOLERANCE_M,
    smooth_bins: int = _DEFAULT_SMOOTH_BINS,
    template_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Constrain a BLAP lap's lateral offsets within left/right track-edge laps.

    The left and right laps are treated as the corridor edges of the track.
    ``mode="translate"`` shifts the whole lap by one constant lateral amount so
    the maximum out-of-corridor excursion is minimized; the lap shape (yaw,
    timing, controls) is preserved exactly. ``mode="clamp"`` clips per-bin
    excursions and smooths the correction instead.

    ``tolerance_m`` widens the corridor on both sides to tolerate small
    excursions (the edge laps typically do not use the full curbs).
    ``smooth_bins`` is the moving-average window used in clamp mode.

    When ``template_path`` is given, the output is packed with that file's
    binary prefix and header instead of the source lap's, which keeps
    car/track/build metadata compatible with a different target vehicle.
    """
    if mode not in ("translate", "clamp"):
        raise ValueError("mode must be translate or clamp")
    if tolerance_m < 0:
        raise ValueError("Corridor tolerance cannot be negative")
    if smooth_bins < 0:
        raise ValueError("Smoothing window cannot be negative")

    source = parse_blap(Path(source_path).expanduser())
    left = parse_blap(Path(left_path).expanduser(), include_prefix=False)
    right = parse_blap(Path(right_path).expanduser(), include_prefix=False)
    _validate_shapes(source, left, right)

    offsets = _sample_offsets(source)
    left_offsets = _sample_offsets(left)
    right_offsets = _sample_offsets(right)
    corridor_lo = np.minimum(left_offsets, right_offsets) - tolerance_m
    corridor_hi = np.maximum(left_offsets, right_offsets) + tolerance_m

    def violation(values: np.ndarray) -> np.ndarray:
        return np.maximum(
            np.maximum(corridor_lo - values, 0.0),
            np.maximum(values - corridor_hi, 0.0),
        )

    if mode == "translate":
        shift = _optimal_translation(offsets, corridor_lo, corridor_hi)
        corrected = offsets + shift
        correction_kind = "translation"
        correction_m = shift
    else:
        clipped = np.clip(offsets, corridor_lo, corridor_hi)
        excursion = offsets - clipped
        smoothed = _moving_average_cyclic(excursion, smooth_bins)
        corrected = offsets - smoothed
        correction_kind = "clamp"
        correction_m = float(np.mean(np.abs(corrected - offsets)))

    before = violation(offsets)
    after = violation(corrected)

    samples = []
    sector_index = 0
    sector_bin = 0
    sector_bins = [
        int(sector["numBins"]) for sector in source["summary"].get("sectors", [])
    ]
    for index, sample in enumerate(source["samples"]):
        while (
            sector_index + 1 < len(sector_bins)
            and sector_bin >= sector_bins[sector_index]
        ):
            sector_index += 1
            sector_bin = 0
        updated = dict(sample)
        updated["lateralOffsetM"] = float(corrected[index])
        samples.append(updated)
        sector_bin += 1

    result = dict(source)
    result["samples"] = samples
    template = None
    if template_path is not None:
        template = parse_blap(Path(template_path).expanduser())
        result["header"] = dict(template["header"])
        result["summary"]["tableVersionCandidate"] = template["summary"].get(
            "tableVersionCandidate",
            template["summary"].get("tableTypeCount", 0),
        )
        for sector, template_sector in zip(
            result["summary"]["sectors"],
            template["summary"].get("sectors", []),
        ):
            sector["startBoundaryVerticalOffsetM"] = template_sector.get(
                "startBoundaryVerticalOffsetM",
                sector.get("startBoundaryVerticalOffsetM", 0.0),
            )
            sector["endBoundaryVerticalOffsetM"] = template_sector.get(
                "endBoundaryVerticalOffsetM",
                sector.get("endBoundaryVerticalOffsetM", 0.0),
            )
            sector["recordFlags"] = template_sector.get(
                "recordFlags", sector.get("recordFlags", 0)
            )

    result["binary"] = dict(source.get("binary", {}))
    raw = pack_blap(result, template_path=template_path)
    result["_raw"] = raw
    result["_diagnostics"] = {
        "correctionKind": correction_kind,
        "correctionM": correction_m,
        "toleranceM": tolerance_m,
        "smoothBins": smooth_bins,
        "beforeMaxViolationM": float(np.max(before)) if len(before) else 0.0,
        "beforeViolatingBins": int(np.sum(before > 1e-9)),
        "afterMaxViolationM": float(np.max(after)) if len(after) else 0.0,
        "afterViolatingBins": int(np.sum(after > 1e-9)),
    }
    return result


def constraint_diagnostics_text(diagnostics: Dict[str, Any]) -> str:
    return (
        "Corridor correction ({}): applied {:.3f} m, tolerance {:.2f} m\n"
        "  before: max {:.3f} m over {:d} bins\n"
        "  after:  max {:.3f} m over {:d} bins".format(
            diagnostics["correctionKind"],
            diagnostics["correctionM"],
            diagnostics["toleranceM"],
            diagnostics["beforeMaxViolationM"],
            diagnostics["beforeViolatingBins"],
            diagnostics["afterMaxViolationM"],
            diagnostics["afterViolatingBins"],
        )
    )
