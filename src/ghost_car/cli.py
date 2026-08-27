"""Single hierarchical command-line interface for ghost-car."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import __version__
from .ac_track import calibrate_track
from .acreplay import parse_acreplay
from .conversion import build_canonical_blap
from .corridor import constrain_blap, constraint_diagnostics_text
from .ibt import (
    average_track_references,
    build_blap_track_reference,
    build_matched_blap_ibt_track_reference,
    combine_alignment_maps,
    fit_ibt_distance_map,
    load_ibt_reference,
)
from .iracing import pack_blap, parse_blap, parse_int
from .kn5 import export_kn5_surface
from .ld_replay import (
    convert_ld_to_acreplay,
    gps_track_to_ac,
    offset_track_calibration,
    validate_track_reference,
)
from .motec import extract_motec_points, parse_channel_overrides
from .replay_writer import _estimate_wheel_position_offsets


def _csv_values(text: str, converter: Callable[[str], Any]) -> List[Any]:
    try:
        values = [converter(value.strip()) for value in text.split(",") if value.strip()]
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error))
    if not values:
        raise argparse.ArgumentTypeError("Expected a comma-separated list")
    return values


def _csv_floats(text: str) -> List[float]:
    return _csv_values(text, float)


def _csv_ints(text: str) -> List[int]:
    return _csv_values(text, int)


def _add_iracing_header_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--driver", help="Driver-name override")
    parser.add_argument("--car", help="Car identifier override")
    parser.add_argument("--track", help="Track identifier override")
    parser.add_argument("--customer-id", type=parse_int, help="Customer ID override")
    parser.add_argument("--version", type=parse_int, help="BLAP version override")
    parser.add_argument("--file-flags", type=parse_int, help="BLAP header flags override")
    parser.add_argument(
        "--build-date",
        action="append",
        help="Build-date field override; repeat up to three times",
    )


def _add_sector_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sector-ends",
        type=_csv_floats,
        help="Comma-separated target sector end distances",
    )
    parser.add_argument(
        "--sector-bins",
        type=_csv_ints,
        help="Comma-separated target sector bin counts",
    )


def _add_iracing_conversion_options(parser: argparse.ArgumentParser) -> None:
    _add_iracing_header_options(parser)
    _add_sector_options(parser)
    parser.add_argument(
        "--yaw-source",
        choices=("tangent", "input", "template"),
        default="tangent",
    )
    parser.add_argument(
        "--pitch-source",
        choices=("tangent", "input", "template"),
        default="tangent",
    )
    parser.add_argument(
        "--roll-source",
        choices=("zero", "input", "template"),
        default="input",
    )
    parser.add_argument("--smoothing-window", type=int, default=35)
    parser.add_argument("--smoothing-order", type=int, default=3)
    parser.add_argument(
        "--smoothing-boundary-mode",
        choices=("reflect", "truncate"),
        default="reflect",
        help="Time-increment smoothing at sector boundaries",
    )
    parser.add_argument(
        "--yaw-smoothing-distance-m",
        type=float,
        default=32.0,
        help="Distance window used to smooth tangent or input yaw; zero disables",
    )
    parser.add_argument("--min-time-step", type=float, default=0.0)
    parser.add_argument(
        "--control-scale",
        choices=("fraction", "percent", "raw"),
        default="percent",
    )
    parser.add_argument("--default-gear", type=int, default=1)
    parser.add_argument("--gear-thresholds-kph", type=_csv_floats, default=[])
    parser.add_argument("--default-brake", type=float, default=0.0)
    parser.add_argument("--default-throttle", type=float, default=0.0)
    parser.add_argument(
        "--clutch-raw",
        "--auxiliary-raw",
        "--system-mask",
        dest="clutch_raw",
        type=parse_int,
        default=0xFF,
        help="Raw clutch byte in flags bits 16..23; other names are legacy aliases",
    )
    parser.add_argument("--brake-light-threshold", type=int, default=10)
    parser.add_argument("--yaw-offset-deg", type=float, default=0.0)
    parser.add_argument("--pitch-offset-deg", type=float, default=0.0)
    parser.add_argument("--roll-offset-deg", type=float, default=0.0)
    parser.add_argument(
        "--lateral-offset-source",
        choices=("auto", "alignment", "template", "zero"),
        default="auto",
        help=(
            "BLAP lateral position: fitted source path, template path, or "
            "iRacing reference spline"
        ),
    )
    parser.add_argument(
        "--no-alignment-yaw-rotation",
        action="store_false",
        dest="apply_alignment_rotation",
        help="Do not rotate source yaw into the inferred BLAP spline frame",
    )
    parser.set_defaults(apply_alignment_rotation=True)
    parser.add_argument(
        "--sector-boundary-source",
        choices=("generated", "template", "zero"),
        default="generated",
        help="Generate start/finish vertical offsets, preserve template, or zero",
    )
    parser.add_argument("--omit-prefix", action="store_true")
    parser.add_argument("--json-indent", type=int, default=2)


def _add_motec_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ldparser-path", help="Directory containing ldparser.py")
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="ROLE=NAME",
        help="Override a MoTeC channel mapping; repeat as needed",
    )
    parser.add_argument("--lap", type=int, help="Specific lap number")
    parser.add_argument(
        "--lap-selection",
        choices=("fastest", "first", "all"),
        default="fastest",
    )
    parser.add_argument("--min-lap-seconds", type=float, default=0.0)
    parser.add_argument("--max-lap-seconds", type=float, default=float("inf"))
    parser.add_argument(
        "--min-lap-distance-ratio",
        type=float,
        default=0.9,
        help="Minimum GPS distance relative to the longest numbered lap",
    )
    parser.add_argument("--max-gps-step-m", type=float)
    parser.add_argument("--gps-step-outlier-factor", type=float, default=20.0)
    parser.add_argument(
        "--speed-unit",
        choices=("auto", "m/s", "km/h", "mph"),
        default="auto",
    )
    parser.add_argument(
        "--heading-unit",
        choices=("degrees", "radians"),
        default="degrees",
    )
    parser.add_argument("--origin-latitude", type=float)
    parser.add_argument("--origin-longitude", type=float)
    parser.add_argument("--origin-altitude", type=float)
    parser.add_argument("--earth-radius-m", type=float, default=6371008.8)
    parser.add_argument("--ldx", help="Explicit companion LDX path")
    parser.add_argument(
        "--ignore-companion-ldx",
        action="store_false",
        dest="use_companion_ldx",
        help="Ignore an automatically discovered companion LDX file",
    )
    parser.set_defaults(use_companion_ldx=True)
    parser.add_argument("--ldx-time-scale", type=float, default=1000000.0)


def _add_ibt_alignment_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--reference-ibt",
        help="iRacing IBT whose GPS path defines the target distance alignment",
    )
    parser.add_argument("--ibt-lap", type=int, help="Complete IBT lap ordinal")
    parser.add_argument(
        "--ibt-lap-selection",
        choices=("fastest", "first"),
        default="fastest",
    )
    parser.add_argument("--ibt-min-lap-seconds", type=float, default=60.0)
    parser.add_argument("--ibt-max-lap-seconds", type=float, default=300.0)
    parser.add_argument("--alignment-sample-spacing-m", type=float, default=2.0)
    parser.add_argument("--icp-max-iterations", type=int, default=30)
    parser.add_argument("--icp-rejection-distance-m", type=float, default=12.0)
    parser.add_argument("--icp-convergence-tolerance", type=float, default=1e-8)
    parser.add_argument(
        "--icp-fixed-scale",
        action="store_false",
        dest="icp_allow_scale",
        help="Constrain the GPS alignment scale to exactly 1.0",
    )
    parser.set_defaults(icp_allow_scale=True)
    parser.add_argument("--alignment-smoothing-distance-m", type=float, default=60.0)
    parser.add_argument("--alignment-endpoint-weight", type=float, default=1000000.0)
    parser.add_argument("--alignment-monotonic-blend", type=float, default=0.01)
    parser.add_argument("--max-alignment-rmse-m", type=float, default=12.0)
    parser.add_argument("--max-alignment-p95-m", type=float, default=20.0)
    parser.add_argument("--lateral-smoothing-distance-m", type=float, default=8.0)
    parser.add_argument(
        "--reference-heading-smoothing-distance-m",
        type=float,
        default=8.0,
    )
    parser.add_argument(
        "--no-reference-loop-closure",
        action="store_false",
        dest="reference_loop_closure",
        help="Do not distribute inferred reference-spline closure error",
    )
    parser.set_defaults(reference_loop_closure=True)


def _set_ld_to_iracing_defaults(parser: argparse.ArgumentParser) -> None:
    """Supply expert defaults for the intentionally small public converter."""
    expert = argparse.ArgumentParser(add_help=False)
    expert.add_argument("--template-body-offset", type=parse_int)
    _add_motec_options(expert)
    _add_ibt_alignment_options(expert)
    _add_iracing_conversion_options(expert)
    parser.set_defaults(
        template=None,
        target_profile=None,
        **vars(expert.parse_args([])),
    )


def _add_profile_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", help="Valid target-car and target-track BLAP/OLAP")
    parser.add_argument("-o", "--output", required=True, help="Output profile JSON")
    parser.add_argument(
        "--reference-lap",
        action="append",
        default=[],
        help=(
            "Additional same-layout BLAP/OLAP used to average the inferred "
            "track spline; repeat as needed"
        ),
    )
    parser.add_argument(
        "--matched-ibt",
        help="IBT containing the same lap as the primary BLAP/OLAP",
    )
    parser.add_argument(
        "--matched-pair",
        action="append",
        nargs=2,
        default=[],
        metavar=("BLAP", "IBT"),
        help="Additional matched BLAP/OLAP and IBT pair; repeat as needed",
    )
    parser.add_argument(
        "--matched-ibt-lap",
        type=int,
        help="Complete IBT lap ordinal for the primary matched pair",
    )


def _add_profile_tuning_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--matched-max-lap-time-difference-s",
        type=float,
        default=0.25,
        help="Maximum BLAP-to-IBT lap-time difference",
    )
    parser.add_argument(
        "--matched-ibt-min-lap-seconds",
        type=float,
        default=60.0,
    )
    parser.add_argument(
        "--matched-ibt-max-lap-seconds",
        type=float,
        default=300.0,
    )
    parser.add_argument(
        "--matched-smoothing-distance-m",
        type=float,
        default=8.0,
    )
    parser.add_argument(
        "--matched-min-lateral-separation-m",
        type=float,
        default=1.0,
    )
    parser.add_argument(
        "--matched-min-usable-fraction",
        type=float,
        default=0.25,
    )
    parser.add_argument(
        "--matched-max-iterations",
        type=int,
        default=5,
    )
    parser.add_argument(
        "--matched-max-fit-rmse-m",
        type=float,
        default=1.0,
    )
    parser.add_argument(
        "--matched-max-fit-p95-m",
        type=float,
        default=2.0,
    )
    parser.add_argument(
        "--reference-heading-smoothing-distance-m",
        type=float,
        default=8.0,
    )
    parser.add_argument(
        "--no-reference-loop-closure",
        action="store_false",
        dest="reference_loop_closure",
        help="Do not distribute inferred reference-spline closure error",
    )
    parser.set_defaults(reference_loop_closure=True)
    parser.add_argument(
        "--max-reference-track-length-difference-m",
        type=float,
        default=0.01,
        help="Maximum allowed track-length difference for an additional lap",
    )
    parser.add_argument("--body-offset", type=parse_int)
    parser.add_argument("--indent", type=int, default=2)


def _set_profile_tuning_defaults(parser: argparse.ArgumentParser) -> None:
    expert = argparse.ArgumentParser(add_help=False)
    _add_profile_tuning_options(expert)
    parser.set_defaults(**vars(expert.parse_args([])))


def _extract_motec(
    args: argparse.Namespace,
    profile_origin: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    if args.min_lap_seconds < 0 or args.max_lap_seconds <= args.min_lap_seconds:
        raise ValueError("lap duration bounds are invalid")
    origin = profile_origin or {}
    return extract_motec_points(
        args.input,
        parser_path=args.ldparser_path,
        channel_overrides=parse_channel_overrides(args.channel),
        target_lap=args.lap,
        lap_selection=args.lap_selection,
        min_lap_seconds=args.min_lap_seconds,
        max_lap_seconds=args.max_lap_seconds,
        min_lap_distance_ratio=args.min_lap_distance_ratio,
        max_gps_step_m=args.max_gps_step_m,
        gps_step_outlier_factor=args.gps_step_outlier_factor,
        speed_unit=args.speed_unit,
        heading_unit=args.heading_unit,
        origin_latitude=args.origin_latitude
        if args.origin_latitude is not None
        else origin.get("latitudeDeg"),
        origin_longitude=args.origin_longitude
        if args.origin_longitude is not None
        else origin.get("longitudeDeg"),
        origin_altitude=args.origin_altitude
        if args.origin_altitude is not None
        else origin.get("altitudeM"),
        earth_radius_m=args.earth_radius_m,
        ldx_path=args.ldx,
        use_companion_ldx=args.use_companion_ldx,
        ldx_time_scale=args.ldx_time_scale,
    )


def _iracing_header_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "driverName": args.driver,
        "carShortName": args.car,
        "trackName": args.track,
        "custId": args.customer_id,
        "version": args.version,
        "flags": args.file_flags,
        "buildDates": args.build_date,
    }


def _build_iracing_lap(
    args: argparse.Namespace,
    points: Sequence[Dict[str, Any]],
    template: Dict[str, Any],
    header_overrides: Dict[str, Any],
    distance_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return build_canonical_blap(
        points,
        template,
        header_overrides=header_overrides,
        sector_ends=args.sector_ends,
        sector_bins=args.sector_bins,
        yaw_source=args.yaw_source,
        pitch_source=args.pitch_source,
        roll_source=args.roll_source,
        smoothing_window=args.smoothing_window,
        smoothing_order=args.smoothing_order,
        smoothing_boundary_mode=args.smoothing_boundary_mode,
        yaw_smoothing_distance_m=args.yaw_smoothing_distance_m,
        reference_heading_smoothing_distance_m=(
            args.reference_heading_smoothing_distance_m
        ),
        min_time_step=args.min_time_step,
        control_scale=args.control_scale,
        default_gear=args.default_gear,
        gear_thresholds_kph=args.gear_thresholds_kph,
        default_brake=args.default_brake,
        default_throttle=args.default_throttle,
        clutch_raw=args.clutch_raw,
        brake_light_threshold=args.brake_light_threshold,
        yaw_offset_deg=args.yaw_offset_deg,
        pitch_offset_deg=args.pitch_offset_deg,
        roll_offset_deg=args.roll_offset_deg,
        distance_map=distance_map,
        lateral_offset_source=args.lateral_offset_source,
        apply_alignment_rotation=args.apply_alignment_rotation,
        sector_boundary_source=args.sector_boundary_source,
    )


_TARGET_PROFILE_FORMAT = "ghost-car-iracing-target-profile-v1"


def _load_target_template(args: argparse.Namespace) -> Dict[str, Any]:
    if args.template:
        return parse_blap(
            Path(args.template).expanduser(),
            body_offset=args.template_body_offset,
            include_prefix=True,
        )
    profile = json.loads(
        Path(args.target_profile).expanduser().read_text(encoding="utf-8")
    )
    if profile.get("format") != _TARGET_PROFILE_FORMAT:
        raise ValueError("Unsupported iRacing target-profile format")
    template = profile.get("template")
    if not isinstance(template, dict):
        raise ValueError("Target profile does not contain a template")
    template = dict(template)
    track_reference = profile.get("trackReference")
    if track_reference is not None:
        if not isinstance(track_reference, dict):
            raise ValueError("Target profile track reference is invalid")
        template["_trackReference"] = track_reference
    return template


def _validate_alignment(
    label: str,
    alignment: Dict[str, Any],
    args: argparse.Namespace,
) -> None:
    if args.max_alignment_rmse_m <= 0 or args.max_alignment_p95_m <= 0:
        raise ValueError("Alignment residual limits must be positive")
    diagnostics = alignment["diagnostics"]
    rmse = float(diagnostics["rmseM"])
    p95 = float(diagnostics["p95ResidualM"])
    if rmse > args.max_alignment_rmse_m or p95 > args.max_alignment_p95_m:
        raise ValueError(
            "{} alignment rejected: RMSE {:.3f}m (limit {:.3f}m), "
            "p95 {:.3f}m (limit {:.3f}m); verify lap selection and x/y/z "
            "channel mapping".format(
                label,
                rmse,
                args.max_alignment_rmse_m,
                p95,
                args.max_alignment_p95_m,
            )
        )


def _handle_iracing_profile(args: argparse.Namespace) -> None:
    if args.matched_ibt_lap is not None and not args.matched_ibt:
        raise ValueError("--matched-ibt-lap requires --matched-ibt")
    if args.max_reference_track_length_difference_m < 0:
        raise ValueError(
            "Maximum reference track-length difference cannot be negative"
        )
    source_path = Path(args.input).expanduser()
    raw = source_path.read_bytes()
    template = parse_blap(
        raw,
        body_offset=args.body_offset,
        include_prefix=True,
    )
    primary_sectors = template.get("summary", {}).get("sectors", [])
    if not primary_sectors:
        raise ValueError("Primary target lap has no sector records")
    primary_track_length = float(primary_sectors[-1]["endDistanceM"]) - float(
        primary_sectors[0].get("startDistanceM", 0.0)
    )
    primary_track_name = template.get("header", {}).get("trackName", "")

    def load_reference_blap(reference_path: Path, primary: bool = False):
        reference_raw = reference_path.read_bytes()
        reference_template = (
            template
            if primary
            else parse_blap(reference_raw, include_prefix=False)
        )
        reference_track_name = reference_template.get("header", {}).get(
            "trackName", ""
        )
        if reference_track_name != primary_track_name:
            raise ValueError(
                "Reference lap track {!r} does not match primary track {!r}".format(
                    reference_track_name,
                    primary_track_name,
                )
            )
        reference_sectors = reference_template.get("summary", {}).get("sectors", [])
        if not reference_sectors:
            raise ValueError("Reference lap {} has no sector records".format(reference_path))
        reference_track_length = float(reference_sectors[-1]["endDistanceM"]) - float(
            reference_sectors[0].get("startDistanceM", 0.0)
        )
        if (
            abs(reference_track_length - primary_track_length)
            > args.max_reference_track_length_difference_m
        ):
            raise ValueError(
                "Reference lap {} track length differs by {:.6f}m; limit is {:.6f}m".format(
                    reference_path,
                    abs(reference_track_length - primary_track_length),
                    args.max_reference_track_length_difference_m,
                )
            )
        return reference_raw, reference_template

    matched_mode = bool(args.matched_ibt or args.matched_pair)
    if matched_mode and not args.matched_ibt:
        raise ValueError("--matched-ibt is required when --matched-pair is used")
    if args.matched_ibt and not args.matched_pair:
        raise ValueError(
            "Matched reconstruction requires --matched-ibt and at least one "
            "--matched-pair BLAP IBT"
        )
    if matched_mode and args.reference_lap:
        raise ValueError(
            "--reference-lap cannot be combined with matched BLAP/IBT reconstruction"
        )

    reference_sources = []
    if matched_mode:
        if args.matched_max_fit_rmse_m <= 0 or args.matched_max_fit_p95_m <= 0:
            raise ValueError("Matched fit residual limits must be positive")
        pair_specs = [
            (source_path, Path(args.matched_ibt).expanduser(), args.matched_ibt_lap)
        ] + [
            (Path(blap_path).expanduser(), Path(ibt_path).expanduser(), None)
            for blap_path, ibt_path in args.matched_pair
        ]
        matched_pairs = []
        ibt_track_name = None
        for pair_index, (blap_path, ibt_path, ibt_lap) in enumerate(pair_specs):
            blap_raw, blap_template = load_reference_blap(
                blap_path,
                primary=pair_index == 0,
            )
            blap_lap_time = float(
                blap_template.get("summary", {}).get("bestLapS", 0.0)
            )
            if blap_lap_time <= 0:
                raise ValueError(
                    "Matched BLAP {} has no valid best-lap time".format(blap_path)
                )
            ibt = load_ibt_reference(
                str(ibt_path),
                target_lap=ibt_lap,
                min_lap_seconds=args.matched_ibt_min_lap_seconds,
                max_lap_seconds=args.matched_ibt_max_lap_seconds,
                target_lap_time_s=blap_lap_time,
                max_lap_time_difference_s=(
                    args.matched_max_lap_time_difference_s
                ),
            )
            current_ibt_track_name = ibt.get("metadata", {}).get("trackName", "")
            if current_ibt_track_name:
                if ibt_track_name is None:
                    ibt_track_name = current_ibt_track_name
                elif current_ibt_track_name != ibt_track_name:
                    raise ValueError(
                        "Matched IBT track {!r} does not match {!r}".format(
                            current_ibt_track_name,
                            ibt_track_name,
                        )
                    )
            matched_pairs.append(
                {
                    "template": blap_template,
                    "ibt": ibt,
                }
            )
            ibt_raw = ibt_path.read_bytes()
            reference_sources.append(
                {
                    "blapFileName": blap_path.name,
                    "blapSha256": hashlib.sha256(blap_raw).hexdigest(),
                    "blapLapTimeS": blap_lap_time,
                    "ibtFileName": ibt_path.name,
                    "ibtSha256": hashlib.sha256(ibt_raw).hexdigest(),
                    "ibtSelectedLap": ibt["selectedLap"],
                    "ibtLapTimeS": ibt["lapTimeS"],
                    "lapTimeDifferenceS": abs(
                        float(ibt["lapTimeS"]) - blap_lap_time
                    ),
                }
            )
        track_reference = build_matched_blap_ibt_track_reference(
            matched_pairs,
            smoothing_distance_m=args.matched_smoothing_distance_m,
            min_lateral_separation_m=args.matched_min_lateral_separation_m,
            min_usable_fraction=args.matched_min_usable_fraction,
            max_track_length_difference_m=(
                args.max_reference_track_length_difference_m
            ),
            max_iterations=args.matched_max_iterations,
        )
        diagnostics = track_reference["diagnostics"]
        if (
            diagnostics["fitRmseM"] > args.matched_max_fit_rmse_m
            or diagnostics["fitP95M"] > args.matched_max_fit_p95_m
        ):
            raise ValueError(
                "Matched reconstruction rejected: RMSE {:.3f}m (limit {:.3f}m), "
                "p95 {:.3f}m (limit {:.3f}m)".format(
                    diagnostics["fitRmseM"],
                    args.matched_max_fit_rmse_m,
                    diagnostics["fitP95M"],
                    args.matched_max_fit_p95_m,
                )
            )
        print(
            "Matched reconstruction: {} pairs, usable {:.1%}, RMSE {:.3f}m, "
            "p95 {:.3f}m, axis-length error {:+.3f}m".format(
                diagnostics["pairCount"],
                diagnostics["usableFraction"],
                diagnostics["fitRmseM"],
                diagnostics["fitP95M"],
                diagnostics["axisLengthDifferenceM"],
            )
        )
    else:
        reference_paths = [source_path] + [
            Path(value).expanduser() for value in args.reference_lap
        ]
        references = []
        for reference_index, reference_path in enumerate(reference_paths):
            reference_raw, reference_template = load_reference_blap(
                reference_path,
                primary=reference_index == 0,
            )
            references.append(
                build_blap_track_reference(
                    reference_template,
                    heading_smoothing_distance_m=(
                        args.reference_heading_smoothing_distance_m
                    ),
                    close_loop=args.reference_loop_closure,
                )
            )
            reference_sources.append(
                {
                    "fileName": reference_path.name,
                    "sha256": hashlib.sha256(reference_raw).hexdigest(),
                }
            )
        track_reference = average_track_references(references)

    profile = {
        "format": _TARGET_PROFILE_FORMAT,
        "sourceSha256": hashlib.sha256(raw).hexdigest(),
        "target": {
            "magic": template["header"]["magic"],
            "trackName": template["header"]["trackName"],
            "carShortName": template["header"]["carShortName"],
            "buildDates": template["header"]["buildDates"],
        },
        "referenceSources": reference_sources,
        "trackReference": track_reference,
        "template": template,
    }
    output_path = Path(args.output).expanduser()
    output_path.write_text(json.dumps(profile, indent=args.indent), encoding="utf-8")
    print("Created target profile {} -> {}".format(source_path, output_path))


def _write_iracing_result(
    args: argparse.Namespace,
    canonical: Dict[str, Any],
) -> None:
    output_path = Path(args.output).expanduser()
    if output_path.suffix.lower() == ".json":
        if args.omit_prefix:
            canonical["binary"].pop("prefixBase64", None)
        output_path.write_text(
            json.dumps(canonical, indent=args.json_indent),
            encoding="utf-8",
        )
    else:
        output_path.write_bytes(pack_blap(canonical))
    print("Converted {} -> {}".format(args.input, output_path))


def _handle_iracing_decode(args: argparse.Namespace) -> None:
    decoded = parse_blap(
        Path(args.input).expanduser(),
        body_offset=args.body_offset,
        include_prefix=not args.omit_prefix,
        brake_light_threshold=args.brake_light_threshold,
    )
    text = json.dumps(decoded, indent=args.indent)
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.write_text(text, encoding="utf-8")
        print("Converted {} -> {}".format(args.input, output_path))
    else:
        print(text)


def _handle_iracing_encode(args: argparse.Namespace) -> None:
    data = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
    header = data.setdefault("header", {})
    overrides = {
        "magic": args.magic,
        "version": args.version,
        "flags": args.file_flags,
        "custId": args.customer_id,
        "driverName": args.driver,
        "carShortName": args.car,
        "trackName": args.track,
        "buildDates": args.build_date,
    }
    header.update({name: value for name, value in overrides.items() if value is not None})
    summary = data.setdefault("summary", {})
    if args.best_lap is not None:
        summary["bestLapS"] = args.best_lap
    sectors = summary.get("sectors", [])
    if args.sector_ends is not None:
        if len(args.sector_ends) != len(sectors):
            raise ValueError("--sector-ends count must match JSON sectors")
        for sector, end in zip(sectors, args.sector_ends):
            sector["endDistanceM"] = end
    if args.sector_bins is not None:
        if len(args.sector_bins) != len(sectors):
            raise ValueError("--sector-bins count must match JSON sectors")
        for sector, bins in zip(sectors, args.sector_bins):
            sector["numBins"] = bins
    output = pack_blap(
        data,
        template_path=Path(args.template).expanduser() if args.template else None,
        body_offset=args.body_offset,
        default_clutch_raw=args.clutch_raw,
    )
    output_path = Path(args.output).expanduser()
    output_path.write_bytes(output)
    print("Converted {} -> {} ({} bytes)".format(args.input, output_path, len(output)))


def _handle_ld_to_iracing(args: argparse.Namespace) -> None:
    motec = _extract_motec(args)
    template = _load_target_template(args)
    metadata = motec["metadata"]
    overrides = _iracing_header_overrides(args)
    sectors = template.get("summary", {}).get("sectors", [])
    if not sectors:
        raise ValueError("iRacing target has no sector records")
    target_track_length = float(sectors[-1]["endDistanceM"]) - float(
        sectors[0].get("startDistanceM", 0.0)
    )

    track_alignment = None
    if args.lateral_offset_source in ("auto", "alignment"):
        track_reference = template.get("_trackReference")
        if track_reference is None:
            track_reference = build_blap_track_reference(
                template,
                heading_smoothing_distance_m=(
                    args.reference_heading_smoothing_distance_m
                ),
                close_loop=args.reference_loop_closure,
            )
        track_alignment = fit_ibt_distance_map(
            motec["points"],
            track_reference,
            target_track_length,
            sample_spacing_m=args.alignment_sample_spacing_m,
            max_iterations=args.icp_max_iterations,
            rejection_distance_m=args.icp_rejection_distance_m,
            convergence_tolerance=args.icp_convergence_tolerance,
            allow_scale=args.icp_allow_scale,
            smoothing_distance_m=args.alignment_smoothing_distance_m,
            endpoint_weight=args.alignment_endpoint_weight,
            monotonic_blend=args.alignment_monotonic_blend,
            lateral_smoothing_distance_m=args.lateral_smoothing_distance_m,
        )
        _validate_alignment("Target spline", track_alignment, args)

    distance_map = track_alignment
    reference = None
    ibt_alignment = None
    if args.reference_ibt:
        reference = load_ibt_reference(
            args.reference_ibt,
            target_lap=args.ibt_lap,
            lap_selection=args.ibt_lap_selection,
            min_lap_seconds=args.ibt_min_lap_seconds,
            max_lap_seconds=args.ibt_max_lap_seconds,
            earth_radius_m=args.earth_radius_m,
        )
        ibt_alignment = fit_ibt_distance_map(
            motec["points"],
            reference,
            target_track_length,
            sample_spacing_m=args.alignment_sample_spacing_m,
            max_iterations=args.icp_max_iterations,
            rejection_distance_m=args.icp_rejection_distance_m,
            convergence_tolerance=args.icp_convergence_tolerance,
            allow_scale=args.icp_allow_scale,
            smoothing_distance_m=args.alignment_smoothing_distance_m,
            endpoint_weight=args.alignment_endpoint_weight,
            monotonic_blend=args.alignment_monotonic_blend,
            lateral_smoothing_distance_m=args.lateral_smoothing_distance_m,
        )
        _validate_alignment("IBT", ibt_alignment, args)
        distance_map = (
            combine_alignment_maps(ibt_alignment, track_alignment)
            if track_alignment is not None
            else ibt_alignment
        )
    canonical = _build_iracing_lap(
        args,
        motec["points"],
        template,
        overrides,
        distance_map=distance_map,
    )
    canonical["source"] = {
        "selectedLap": motec["selectedLap"],
        "frequencyHz": motec["frequencyHz"],
        "metadata": metadata,
    }
    if track_alignment is not None:
        canonical["source"]["targetSplineAlignment"] = {
            "diagnostics": track_alignment["diagnostics"],
        }
        diagnostics = track_alignment["diagnostics"]
        print(
            "Target-spline alignment: RMSE {:.3f}m, p95 {:.3f}m, "
            "lateral [{:.3f}, {:.3f}]m".format(
                diagnostics["rmseM"],
                diagnostics["p95ResidualM"],
                diagnostics["lateralOffsetMinM"],
                diagnostics["lateralOffsetMaxM"],
            )
        )
    if reference is not None and ibt_alignment is not None:
        canonical["source"]["ibtAlignment"] = {
            "selectedLap": reference["selectedLap"],
            "lapTimeS": reference["lapTimeS"],
            "origin": reference["origin"],
            "metadata": reference["metadata"],
            "diagnostics": ibt_alignment["diagnostics"],
        }
        diagnostics = ibt_alignment["diagnostics"]
        print(
            "IBT alignment lap {} ({:.3f}s): RMSE {:.3f}m, p95 {:.3f}m, "
            "scale {:.6f}, rotation {:.3f}deg".format(
                reference["selectedLap"],
                reference["lapTimeS"],
                diagnostics["rmseM"],
                diagnostics["p95ResidualM"],
                diagnostics["scale"],
                diagnostics["rotationDeg"],
            )
        )
    _write_iracing_result(args, canonical)


def _handle_iracing_constrain(args: argparse.Namespace) -> None:
    result = constrain_blap(
        source_path=args.input,
        left_path=args.left,
        right_path=args.right,
        mode=args.mode,
        tolerance_m=args.tolerance_m,
        smooth_bins=args.smooth_bins,
        template_path=args.template,
    )
    output_path = Path(args.output).expanduser()
    output_path.write_bytes(result["_raw"])
    diagnostics = result["_diagnostics"]
    print("Constrained {} -> {}".format(args.input, output_path))
    print(constraint_diagnostics_text(diagnostics))


def _handle_replay_inspect(args: argparse.Namespace) -> None:
    decoded = parse_acreplay(
        Path(args.input).expanduser(),
        max_frames=args.max_frames,
        include_raw_frames=args.include_raw_frames,
    )
    csp = decoded.get("csp")
    if csp is not None:
        for record in csp.get("records", []):
            payload = record.pop("payload", b"")
            record["payloadBase64"] = base64.b64encode(payload).decode("ascii")
    text = json.dumps(decoded, indent=args.indent, ensure_ascii=False)
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.write_text(text, encoding="utf-8")
        print("Converted {} -> {}".format(args.input, output_path))
    else:
        print(text)


def _handle_replay_doctor(args: argparse.Namespace) -> None:
    """Validate replay-conversion inputs before starting a long conversion."""
    errors = []
    warnings = []
    template_path = Path(args.template).expanduser()
    ld_path = Path(args.input).expanduser()
    if not template_path.is_file():
        errors.append("template does not exist: {}".format(template_path))
    if not ld_path.is_file():
        errors.append("LD input does not exist: {}".format(ld_path))
    decoded = None
    if template_path.is_file():
        try:
            decoded = parse_acreplay(
                template_path, max_frames=0, include_raw_frames=True
            )
            if not decoded["cars"]:
                errors.append("template contains no cars")
            else:
                car = decoded["cars"][min(args.car, len(decoded["cars"]) - 1)]
                raw_frames = [
                    bytes.fromhex(frame["rawHex"]) for frame in car["frames"]
                ]
                _estimate_wheel_position_offsets(raw_frames)
        except Exception as error:
            errors.append("template validation failed: {}".format(error))

    points = None
    if ld_path.is_file():
        try:
            if args.compare_laps:
                point_sets = [
                    extract_motec_points(ld_path, target_lap=lap)
                    for lap in args.compare_laps
                ]
                points = point_sets[0]
            else:
                # Diagnose the fastest timed lap; a full session can include
                # pits/out-laps that are intentionally outside the circuit.
                point_sets = [extract_motec_points(ld_path)]
                points = point_sets[0]
            if len(points["points"]) < 2 or points["lapTimeS"] <= 0.0:
                errors.append("LD contains no usable timed trajectory")
            if points["frequencyHz"] <= 0.0:
                errors.append("LD has no positive sample frequency")
        except Exception as error:
            errors.append("LD validation failed: {}".format(error))

    track_ref = None
    surfaces = []
    if args.gps_track:
        try:
            from .ac_track import load_track_package

            track_ref, _, surfaces, _ = load_track_package(args.gps_track)
            if decoded is not None:
                validate_track_reference(decoded, track_ref)
            if points is not None:
                alignment_values = [
                    gps_track_to_ac(item, track_ref)[1] for item in point_sets
                ]
                worst = max(alignment_values)
                if worst > args.max_alignment_rmse_m:
                    errors.append(
                        "GPS-to-AC alignment RMSE {:.2f}m exceeds {:.2f}m".format(
                            worst, args.max_alignment_rmse_m
                        )
                    )
                else:
                    print("PASS alignment RMSE: {:.3f}m".format(worst))
        except Exception as error:
            errors.append("track validation failed: {}".format(error))
    elif args.height_mode in ("track", "kn5"):
        errors.append("--gps-track is required for --height-mode {}".format(args.height_mode))

    if args.height_mode == "kn5":
        requested = args.track_surface or surfaces
        missing = [Path(item) for item in requested if not Path(item).is_file()]
        if missing:
            errors.append("missing KN5 surface: {}".format(missing[0]))
        elif not requested:
            errors.append("KN5 mode has no packaged or explicit surface")
    if args.compare_laps and not args.gps_track:
        errors.append(
            "comparison requires --gps-track for GPS-to-AC mapping "
            "(including driven-distance mode)"
        )
    if args.compare_laps and args.compare_sync == "driven-distance":
        warnings.append(
            "driven-distance uses each LD lap's cumulative distance; it still "
            "needs --gps-track for GPS-to-AC mapping"
        )

    if decoded is not None:
        print("PASS template: {} car(s), {} frame(s)".format(
            decoded["header"]["numCars"], decoded["header"]["numFrames"]
        ))
    if points is not None:
        print("PASS LD: {:.2f}s, {:.2f}Hz, {} samples".format(
            points["lapTimeS"], points["frequencyHz"], len(points["points"])
        ))
    for warning in warnings:
        print("WARN {}".format(warning))
    if errors:
        for error in errors:
            print("FAIL {}".format(error))
        raise SystemExit(2)
    print("PASS replay inputs are ready")


def _handle_replay_export_kn5_surface(args: argparse.Namespace) -> None:
    result = export_kn5_surface(
        Path(args.input).expanduser(),
        Path(args.output).expanduser(),
        mesh_pattern=args.mesh_pattern,
    )
    print(
        "Exported {meshCount} KN5 meshes, {vertexCount} vertices, "
        "{triangleCount} triangles -> {output}".format(**result)
    )


def _handle_replay_calibrate_track(args: argparse.Namespace) -> None:
    result = calibrate_track(
        reference_path=Path(args.reference).expanduser(),
        ld_paths=[Path(item).expanduser() for item in args.input],
        laps=args.lap,
        reference_car=args.reference_car,
        reference_lap=args.reference_lap,
        track_name=args.track_name,
        layout=args.layout,
        alignment_samples=args.alignment_samples,
        allow_scale=args.allow_scale,
        max_scale_error=args.max_scale_error,
        max_rmse_m=args.max_rmse_m,
        parser_path=args.ldparser_path,
        channel_overrides=parse_channel_overrides(args.channel),
    )
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    calibration = result["calibration"]
    print(
        "Calibrated {sourceCount} LD lap(s): ordered RMSE {orderedRmseM:.3f}m, "
        "nearest P95 {nearestP95M:.3f}m, scale {scale:.6f} -> {output}".format(
            output=output, **calibration
        )
    )
    for index, source in enumerate(calibration["sources"], 1):
        print(
            "  Source {index}: lap {selectedLap}, {lapTimeS:.3f}s, nearest "
            "RMSE {nearestRmseM:.3f}m, P95 {nearestP95M:.3f}m".format(
                index=index, **source
            )
        )


def _handle_replay_convert(args: argparse.Namespace) -> None:
    result = convert_ld_to_acreplay(
        template_path=Path(args.template).expanduser(),
        ld_path=Path(args.input).expanduser(),
        output_path=Path(args.output).expanduser(),
        car_index=args.car,
        lap=args.lap,
        session=args.session,
        compare_laps=args.compare_laps,
        compare_skins=args.compare_skins,
        compare_sync=args.compare_sync,
        channel_overrides=parse_channel_overrides(args.channel),
        gps_track_path=Path(args.gps_track).expanduser() if args.gps_track else None,
        height_mode=args.height_mode,
        height_offset_m=args.height_offset_m,
        position_smoothing_s=args.position_smoothing_s,
        yaw_smoothing_s=args.yaw_smoothing_s,
        wheel_steer_multiplier=args.wheel_steer_multiplier,
        track_surface_path=args.track_surface,
    )
    if result["mode"] == "compare":
        print(
            "Converted LD laps {laps} into {carCount} {compareSync} cars "
            "({lapTimeS:.2f}s) -> {output} ({frameCount} frames)".format(
                laps=", ".join(str(item) for item in result["selectedLaps"]),
                **result
            )
        )
        for item in result["perCar"]:
            print(
                "  Lap {selectedLap}: source {lapTimeS:.2f}s, alignment RMSE "
                "{horizontalAlignmentRmseM:.2f}m".format(**item)
            )
    elif result["mode"] == "session":
        print(
            "Converted full LD session ({lapTimeS:.2f}s) -> {output} "
            "({frameCount} frames)".format(**result)
        )
        segments = result.get("sessionSegments", [])
        out_lap = next(
            (item for item in segments if item["kind"] == "out-lap"), None
        )
        timed_laps = [
            item for item in segments if item["kind"] == "timed-lap"
        ]
        in_lap = next(
            (item for item in segments if item["kind"] == "in-lap"), None
        )
        if out_lap is not None:
            print(
                "  Included out lap: {durationS:.2f}s "
                "({startS:.2f}-{endS:.2f}s)".format(**out_lap)
            )
        if timed_laps:
            print(
                "  Included timed laps: {} ({:.2f}s total)".format(
                    len(timed_laps),
                    sum(item["durationS"] for item in timed_laps),
                )
            )
        if in_lap is not None:
            print(
                "  Included in lap: {durationS:.2f}s "
                "({startS:.2f}-{endS:.2f}s)".format(**in_lap)
            )
        if not segments:
            print(
                "  Included first through last LD sample; no LDX lap boundaries found"
            )
    else:
        print(
            "Converted LD lap {selectedLap} ({lapTimeS:.2f}s) -> {output} "
            "({frameCount} frames)".format(**result)
        )
    print(
        "Alignment: {alignmentMethod}, horizontal RMSE "
        "{horizontalAlignmentRmseM:.2f}m".format(**result)
    )
    print(
        "Height: {heightMode}, datum {verticalDatumOffsetM:+.2f}m, "
        "before RMSE {beforeVerticalRmseM:.2f}m, after RMSE "
        "{afterVerticalRmseM:.2f}m, offset {heightOffsetM:+.2f}m".format(
            **result
        )
    )
    if result["heightMode"] == "kn5":
        print(
            "KN5 surface: {surfaceMatchedPoints}/{surfaceTotalPoints} points "
            "({surfaceMatchRatio:.2%}), body clearance "
            "{surfaceBodyClearanceM:.3f}m".format(**result)
        )
    print(
        "Position smoothing: {positionSmoothingS:.2f}s / "
        "{positionSmoothingSamples} samples, RMS adjustment "
        "{positionSmoothingRmsM:.2f}m, P95 {positionSmoothingP95M:.2f}m".format(
            **result
        )
    )
    print(
        "Front-wheel steering visual multiplier: "
        "{wheelSteerMultiplier:.2f}x".format(**result)
    )


def _handle_replay_offset_track(args: argparse.Namespace) -> None:
    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser()
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Track offset output must be a new file")
    with input_path.open(encoding="utf-8") as handle:
        track_ref = json.load(handle)
    adjusted = offset_track_calibration(
        track_ref, x_m=args.x_m, z_m=args.z_m
    )
    output_path.write_text(
        json.dumps(adjusted, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    cumulative = adjusted["calibration"]["manualOffsetAcM"]
    print(
        "Wrote {output}; applied AC offset X {x_m:+.3f}m, Z {z_m:+.3f}m "
        "(cumulative X {total_x:+.3f}m, Z {total_z:+.3f}m)".format(
            output=output_path,
            x_m=args.x_m,
            z_m=args.z_m,
            total_x=cumulative["x"],
            total_z=cumulative["z"],
        )
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghost-car",
        description="Decode, encode, and generate iRacing BLAP/OLAP files.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    commands = parser.add_subparsers(dest="command", required=True)

    convert = commands.add_parser(
        "convert",
        help="Convert a MoTeC LD lap to iRacing BLAP/OLAP",
    )
    _set_ld_to_iracing_defaults(convert)
    convert.add_argument("input", help="Input MoTeC .ld path")
    convert.add_argument("-o", "--output", required=True, help="Output .blap/.olap path")
    target = convert.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--profile",
        "--target-profile",
        dest="target_profile",
        help="Reusable target profile (recommended)",
    )
    target.add_argument("--template", help="Valid target-car and target-track BLAP/OLAP")
    convert.add_argument("--lap", type=int, help="Specific numbered lap; defaults to fastest")
    convert.add_argument(
        "--reference-ibt",
        help="Optional target-layout IBT for longitudinal alignment",
    )
    convert.add_argument("--driver", help="Driver-name override")
    convert.set_defaults(handler=_handle_ld_to_iracing)

    inspect = commands.add_parser("inspect", help="Decode BLAP/OLAP to Canonical JSON")
    inspect.add_argument("input", help="Input .blap/.olap path")
    inspect.add_argument("-o", "--output", help="Output JSON path; defaults to stdout")
    inspect.add_argument("--indent", type=int, default=2, help="JSON indentation")
    inspect.add_argument("--body-offset", type=parse_int, help="Override sample body offset")
    inspect.add_argument("--omit-prefix", action="store_true")
    inspect.add_argument("--brake-light-threshold", type=int, default=10)
    inspect.set_defaults(handler=_handle_iracing_decode)

    replay = commands.add_parser(
        "replay",
        help="Inspect and generate Assetto Corsa .acreplay files",
    )
    replay_commands = replay.add_subparsers(dest="replay_command", required=True)
    replay_inspect = replay_commands.add_parser(
        "inspect",
        help="Decode an .acreplay header, cars, and CSP extension records",
    )
    replay_inspect.add_argument("input", help="Input .acreplay path")
    replay_inspect.add_argument("-o", "--output", help="Output JSON path; defaults to stdout")
    replay_inspect.add_argument("--indent", type=int, default=2, help="JSON indentation")
    replay_inspect.add_argument(
        "--max-frames",
        type=int,
        default=1000,
        help="Decode at most this many physics frames per car (0 = all)",
    )
    replay_inspect.add_argument(
        "--include-raw-frames",
        action="store_true",
        help="Attach raw 256-byte hex per decoded frame",
    )
    replay_inspect.set_defaults(handler=_handle_replay_inspect)

    replay_doctor = replay_commands.add_parser(
        "doctor",
        help="Validate replay template, LD, calibration, and KN5 inputs",
    )
    replay_doctor.add_argument("template", help="Native same-layout .acreplay template")
    replay_doctor.add_argument("input", help="Input MoTeC .ld path")
    replay_doctor.add_argument("--car", type=int, default=0, help="Template car index")
    replay_doctor.add_argument("--gps-track", help="Calibrated track package or track.json")
    replay_doctor.add_argument(
        "--height-mode", choices=("track", "kn5", "gps-offset", "gps"), default="track"
    )
    replay_doctor.add_argument("--track-surface", action="append")
    replay_doctor.add_argument("--compare-laps", type=int, nargs="+")
    replay_doctor.add_argument(
        "--compare-sync", choices=("time", "progress", "driven-distance"), default="time"
    )
    replay_doctor.add_argument(
        "--max-alignment-rmse-m", type=float, default=8.0,
        help="Fail when GPS-to-AC nearest-path RMSE exceeds this value",
    )
    replay_doctor.set_defaults(handler=_handle_replay_doctor)

    replay_export_surface = replay_commands.add_parser(
        "export-kn5-surface",
        help="Extract road-height geometry from KN5 using pure Python",
    )
    replay_export_surface.add_argument("input", help="Input Assetto Corsa .kn5")
    replay_export_surface.add_argument(
        "-o", "--output", required=True, help="Output .gcsurface path"
    )
    replay_export_surface.add_argument(
        "--mesh-pattern",
        default=r"^(1ROAD|1PIT)",
        help="Case-insensitive regex selecting KN5 mesh names",
    )
    replay_export_surface.set_defaults(handler=_handle_replay_export_kn5_surface)

    replay_calibrate = replay_commands.add_parser(
        "calibrate-track",
        help="Fit one shared GPS-to-AC transform from one or more complete LD laps",
    )
    replay_calibrate.add_argument(
        "reference",
        help="Native full-lap .acreplay, calibrated track.json, or track package",
    )
    replay_calibrate.add_argument(
        "input", nargs="+", help="One or more MoTeC .ld files"
    )
    replay_calibrate.add_argument(
        "-o", "--output", required=True, help="Output calibrated track.json"
    )
    replay_calibrate.add_argument(
        "--lap",
        type=int,
        action="append",
        help="LD lap number; repeat per input, or specify once for all inputs",
    )
    replay_calibrate.add_argument(
        "--reference-car", type=int, default=0, help="Zero-based replay car index"
    )
    replay_calibrate.add_argument(
        "--reference-lap", type=int, help="Raw currentLap value in reference replay"
    )
    replay_calibrate.add_argument("--track-name", help="AC track identifier override")
    replay_calibrate.add_argument("--layout", help="AC layout identifier override")
    replay_calibrate.add_argument(
        "--alignment-samples", type=int, default=500, help="Closed-path fit samples"
    )
    replay_calibrate.add_argument(
        "--allow-scale",
        action="store_true",
        help="Fit horizontal scale in addition to rotation/reflection/translation",
    )
    replay_calibrate.add_argument(
        "--max-scale-error",
        type=float,
        default=0.03,
        help="Maximum absolute fitted scale error from 1.0",
    )
    replay_calibrate.add_argument(
        "--max-rmse-m",
        type=float,
        default=8.0,
        help="Reject an ordered trajectory fit above this RMSE",
    )
    replay_calibrate.add_argument(
        "--ldparser-path", help="Directory containing ldparser.py"
    )
    replay_calibrate.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="ROLE=NAME",
        help="Override an LD channel; repeat for multiple roles",
    )
    replay_calibrate.set_defaults(handler=_handle_replay_calibrate_track)

    replay_offset = replay_commands.add_parser(
        "offset-track",
        help="Create a track JSON with a manual AC-world X/Z correction",
    )
    replay_offset.add_argument("input", help="Input calibrated track.json")
    replay_offset.add_argument(
        "-o", "--output", required=True, help="New corrected track.json path"
    )
    replay_offset.add_argument(
        "--x-m", type=float, default=0.0, help="Metres added to AC world X"
    )
    replay_offset.add_argument(
        "--z-m", type=float, default=0.0, help="Metres added to AC world Z"
    )
    replay_offset.set_defaults(handler=_handle_replay_offset_track)

    replay_convert = replay_commands.add_parser(
        "convert",
        help="Convert MoTeC LD laps or sessions using a native replay template",
    )
    replay_convert.add_argument("template", help="Native same-layout .acreplay template")
    replay_convert.add_argument("input", help="Input MoTeC .ld path")
    replay_convert.add_argument("-o", "--output", required=True, help="Output .acreplay path")
    replay_convert.add_argument(
        "--car",
        type=int,
        default=0,
        help="Zero-based template car index to replace",
    )
    replay_target = replay_convert.add_mutually_exclusive_group()
    replay_target.add_argument(
        "--lap",
        type=int,
        help="Specific numbered LD lap; defaults to fastest",
    )
    replay_target.add_argument(
        "--session",
        action="store_true",
        help="Convert the complete LD recording from session start to end",
    )
    replay_target.add_argument(
        "--compare-laps",
        type=int,
        nargs="+",
        metavar="LAP",
        help=(
            "Put 2-16 laps into one replay as comparison cars; "
            "requires --gps-track"
        ),
    )
    replay_convert.add_argument(
        "--compare-skins",
        nargs="+",
        metavar="SKIN",
        help="Optional skin IDs corresponding to --compare-laps",
    )
    replay_convert.add_argument(
        "--compare-sync",
        choices=("time", "progress", "driven-distance"),
        default="time",
        help=(
            "Comparison timing: actual lap times (default), equal shared-track "
            "progress, or equal per-lap driven distance"
        ),
    )
    replay_convert.add_argument(
        "--gps-track",
        help="Calibrated track.json or package directory with surfaces",
    )
    replay_convert.add_argument(
        "--height-mode",
        choices=("track", "kn5", "gps-offset", "gps"),
        default="track",
        help=(
            "Vertical mapping: AC reference path (default), KN5-derived road "
            "surface, GPS profile with datum correction, or raw mapped GPS"
        ),
    )
    replay_convert.add_argument(
        "--track-surface",
        action="append",
        help=(
            "KN5-derived .gcsurface; repeat for main track and terrain files; "
            "required by --height-mode kn5"
        ),
    )
    replay_convert.add_argument(
        "--height-offset-m",
        type=float,
        default=0.0,
        help="Final vertical body offset in metres after height mapping",
    )
    replay_convert.add_argument(
        "--position-smoothing-s",
        type=float,
        default=1.0,
        help="Zero-phase GPS X/Z and yaw smoothing window in seconds",
    )
    replay_convert.add_argument(
        "--yaw-smoothing-s",
        type=float,
        default=1.0,
        help=(
            "Zero-phase body-yaw smoothing window in seconds; kept separate "
            "from GPS position smoothing to preserve lap line differences"
        ),
    )
    replay_convert.add_argument(
        "--wheel-steer-multiplier",
        type=float,
        default=1.0,
        help=(
            "Visual multiplier for calibrated front-wheel yaw; leaves the "
            "recorded steering field unchanged"
        ),
    )
    replay_convert.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="ROLE=NAME",
        help="Override an LD channel; repeat for multiple roles",
    )
    replay_convert.set_defaults(handler=_handle_replay_convert)

    profile = commands.add_parser("profile", help="Manage reusable target profiles")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    create_profile = profile_commands.add_parser(
        "create",
        help="Create a target profile from a valid BLAP/OLAP",
    )
    _add_profile_inputs(create_profile)
    _set_profile_tuning_defaults(create_profile)
    create_profile.set_defaults(handler=_handle_iracing_profile)

    advanced = commands.add_parser("advanced", help="Expert and format-engineering commands")
    advanced_commands = advanced.add_subparsers(dest="advanced_command", required=True)
    advanced_convert = advanced_commands.add_parser(
        "convert",
        help="Convert MoTeC LD with all expert controls",
    )
    advanced_convert.add_argument("input", help="Input MoTeC .ld path")
    advanced_convert.add_argument("-o", "--output", required=True)
    target = advanced_convert.add_mutually_exclusive_group(required=True)
    target.add_argument("--template", help="Valid target-car and target-track BLAP/OLAP")
    target.add_argument(
        "--profile",
        "--target-profile",
        dest="target_profile",
        help="Reusable profile created by 'ghost-car profile create'",
    )
    advanced_convert.add_argument("--template-body-offset", type=parse_int)
    _add_motec_options(advanced_convert)
    _add_ibt_alignment_options(advanced_convert)
    _add_iracing_conversion_options(advanced_convert)
    advanced_convert.set_defaults(handler=_handle_ld_to_iracing)

    advanced_profile = advanced_commands.add_parser(
        "profile",
        help="Create a target profile with all expert controls",
    )
    _add_profile_inputs(advanced_profile)
    _add_profile_tuning_options(advanced_profile)
    advanced_profile.set_defaults(handler=_handle_iracing_profile)

    advanced_constrain = advanced_commands.add_parser(
        "constrain",
        help="Constrain a lap within left/right track-edge boundary laps",
    )
    advanced_constrain.add_argument("input", help="Input .blap/.olap path to constrain")
    advanced_constrain.add_argument(
        "--left", required=True, help="Left track-edge boundary .blap/.olap"
    )
    advanced_constrain.add_argument(
        "--right", required=True, help="Right track-edge boundary .blap/.olap"
    )
    advanced_constrain.add_argument("-o", "--output", required=True)
    advanced_constrain.add_argument(
        "--mode",
        choices=("translate", "clamp"),
        default="translate",
        help=(
            "translate: shift the whole lap laterally to minimize the maximum "
            "out-of-corridor excursion (shape preserved); "
            "clamp: clip per-bin excursions and smooth the correction"
        ),
    )
    advanced_constrain.add_argument(
        "--tolerance-m",
        type=float,
        default=1.0,
        help="Extra corridor width on each side in metres",
    )
    advanced_constrain.add_argument(
        "--smooth-bins",
        type=int,
        default=6,
        help="Moving-average window (bins) used in clamp mode",
    )
    advanced_constrain.add_argument(
        "--template",
        help="Optional BLAP used as the output binary prefix and header template",
    )
    advanced_constrain.set_defaults(handler=_handle_iracing_constrain)

    encode = advanced_commands.add_parser(
        "encode",
        help=argparse.SUPPRESS,
    )
    encode.add_argument("input", help="Input Canonical JSON path")
    encode.add_argument("-o", "--output", required=True, help="Output .blap/.olap path")
    encode.add_argument("--template", help="Prefix template when JSON omits its prefix")
    encode.add_argument("--body-offset", type=parse_int, help="Override binary body offset")
    encode.add_argument("--magic", help="Four-byte file magic override")
    _add_iracing_header_options(encode)
    encode.add_argument("--best-lap", type=float, help="Best lap time in seconds")
    _add_sector_options(encode)
    encode.add_argument(
        "--clutch-raw",
        "--auxiliary-raw",
        "--system-mask",
        dest="clutch_raw",
        type=parse_int,
        default=0xFF,
        help="Raw clutch byte in flags bits 16..23; other names are legacy aliases",
    )
    encode.set_defaults(handler=_handle_iracing_encode)
    advanced_commands._choices_actions[:] = [
        action
        for action in advanced_commands._choices_actions
        if action.dest != "encode"
    ]
    advanced_commands.metavar = "{convert,profile,constrain}"

    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (ImportError, OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
