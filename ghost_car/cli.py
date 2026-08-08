"""Single hierarchical command-line interface for ghost-car."""

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import __version__
from .iracing import pack_blap, parse_blap, parse_int
from .conversion import build_canonical_blap
from .motec import extract_motec_points, parse_channel_overrides


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
    parser.add_argument("--system-mask", type=parse_int, default=0xFF)
    parser.add_argument("--brake-light-threshold", type=int, default=10)
    parser.add_argument("--yaw-offset-deg", type=float, default=0.0)
    parser.add_argument("--pitch-offset-deg", type=float, default=0.0)
    parser.add_argument("--roll-offset-deg", type=float, default=0.0)
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
        min_time_step=args.min_time_step,
        control_scale=args.control_scale,
        default_gear=args.default_gear,
        gear_thresholds_kph=args.gear_thresholds_kph,
        default_brake=args.default_brake,
        default_throttle=args.default_throttle,
        system_raw=args.system_mask,
        brake_light_threshold=args.brake_light_threshold,
        yaw_offset_deg=args.yaw_offset_deg,
        pitch_offset_deg=args.pitch_offset_deg,
        roll_offset_deg=args.roll_offset_deg,
    )


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
        default_system_raw=args.system_mask,
    )
    output_path = Path(args.output).expanduser()
    output_path.write_bytes(output)
    print("Converted {} -> {} ({} bytes)".format(args.input, output_path, len(output)))


def _handle_ld_to_iracing(args: argparse.Namespace) -> None:
    motec = _extract_motec(args)
    template = parse_blap(
        Path(args.template).expanduser(),
        body_offset=args.template_body_offset,
        include_prefix=True,
    )
    metadata = motec["metadata"]
    overrides = _iracing_header_overrides(args)
    if overrides["driverName"] is None:
        overrides["driverName"] = metadata["driverName"] or None
    if overrides["carShortName"] is None:
        overrides["carShortName"] = metadata["carName"] or None
    if overrides["trackName"] is None:
        overrides["trackName"] = metadata["trackName"] or None
    canonical = _build_iracing_lap(args, motec["points"], template, overrides)
    canonical["source"] = {
        "selectedLap": motec["selectedLap"],
        "frequencyHz": motec["frequencyHz"],
    }
    _write_iracing_result(args, canonical)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghost-car",
        description="Decode, encode, and generate iRacing BLAP/OLAP files.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    domains = parser.add_subparsers(dest="domain", required=True)

    iracing = domains.add_parser("iracing", help="Decode or encode iRacing BLAP/OLAP")
    iracing_commands = iracing.add_subparsers(dest="iracing_command", required=True)
    decode = iracing_commands.add_parser("decode", help="Decode BLAP/OLAP to Canonical JSON")
    decode.add_argument("input", help="Input .blap/.olap path")
    decode.add_argument("-o", "--output", help="Output JSON path; defaults to stdout")
    decode.add_argument("--indent", type=int, default=2, help="JSON indentation")
    decode.add_argument("--body-offset", type=parse_int, help="Override sample body offset")
    decode.add_argument("--omit-prefix", action="store_true")
    decode.add_argument("--brake-light-threshold", type=int, default=10)
    decode.set_defaults(handler=_handle_iracing_decode)

    encode = iracing_commands.add_parser("encode", help="Encode Canonical JSON to BLAP/OLAP")
    encode.add_argument("input", help="Input Canonical JSON path")
    encode.add_argument("-o", "--output", required=True, help="Output .blap/.olap path")
    encode.add_argument("--template", help="Prefix template when JSON omits its prefix")
    encode.add_argument("--body-offset", type=parse_int, help="Override binary body offset")
    encode.add_argument("--magic", help="Four-byte file magic override")
    _add_iracing_header_options(encode)
    encode.add_argument("--best-lap", type=float, help="Best lap time in seconds")
    _add_sector_options(encode)
    encode.add_argument("--system-mask", type=parse_int, default=0xFF)
    encode.set_defaults(handler=_handle_iracing_encode)

    convert = domains.add_parser("convert", help="Convert between ghost-car formats")
    conversions = convert.add_subparsers(dest="conversion", required=True)

    ld_to_iracing = conversions.add_parser(
        "ld-to-iracing",
        help="Convert MoTeC LD telemetry to BLAP/OLAP or JSON",
    )
    ld_to_iracing.add_argument("input", help="Input MoTeC .ld path")
    ld_to_iracing.add_argument("-o", "--output", required=True)
    ld_to_iracing.add_argument("--template", required=True, help="Target-track BLAP/OLAP template")
    ld_to_iracing.add_argument("--template-body-offset", type=parse_int)
    _add_motec_options(ld_to_iracing)
    _add_iracing_conversion_options(ld_to_iracing)
    ld_to_iracing.set_defaults(handler=_handle_ld_to_iracing)

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
