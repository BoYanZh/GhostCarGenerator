# Ghost Car

Ghost Car is a Python package and hierarchical CLI for iRacing BLAP/OLAP files.
It supports lossless binary-to-JSON round trips and conversion of MoTeC LD
telemetry into an iRacing lap using a target-track BLAP/OLAP template.

Assetto Corsa ghost files are intentionally not supported. The verified format
observations and the engineering reasons for that decision are recorded in
[docs/assetto-corsa-ghost-notes.md](docs/assetto-corsa-ghost-notes.md).

## Requirements

- Python 3.8 or newer
- `numpy` and the bundled `ldparser` submodule for MoTeC LD input

~~~powershell
git submodule update --init
python -m pip install -e ".[motec]"
~~~

The installed `ghost-car` command and `python -m ghost_car` are equivalent.

## CLI

~~~text
ghost-car
├── iracing
│   ├── decode
│   ├── encode
│   └── profile
└── convert
    └── ld-to-iracing
~~~

Every leaf command provides its complete option reference:

~~~powershell
ghost-car iracing decode --help
ghost-car iracing encode --help
ghost-car iracing profile --help
ghost-car convert ld-to-iracing --help
~~~

## Lossless BLAP/OLAP conversion

~~~powershell
ghost-car iracing decode input.blap -o lap.json
ghost-car iracing encode lap.json -o rebuilt.blap
~~~

The canonical JSON retains the opaque binary prefix by default, enabling an
exact round trip for observed iRacing v3 files. Use `--omit-prefix` when the
prefix is not needed; encoding such JSON requires `--template`.

The second sample float is a signed lateral offset in metres relative to the
iRacing track spline. It is exposed as `lateralOffsetM`; `deltaS` is accepted
only when reading JSON produced by ghost-car 0.1.0 or older.

## Reusable iRacing target profiles

A MoTeC log does not contain iRacing's car/track resource versions, sector
grid, internal track spline, or packed driver/car metadata. A valid target-car
and target-layout BLAP/OLAP is therefore required once:

~~~powershell
ghost-car iracing profile target.blap -o target-profile.json
~~~

Multiple official laps from the exact same layout can reduce the yaw/slip bias
of any single driven lap when inferring iRacing's private track spline:

~~~powershell
ghost-car iracing profile target.blap `
  --reference-lap target.olap `
  --reference-lap other-car.blap `
  --reference-lap other-car.olap `
  -o target-profile.json
~~~

The primary input alone supplies the verified binary prefix, sector grid, and
target car/track metadata. Additional laps affect only the averaged reference
spline. Their track identifiers and lengths must match the primary layout.
The profile replaces a live template during later conversions. LD vehicle,
venue, and driver strings are recorded as provenance but do not replace target
metadata unless an explicit header override is supplied.

## MoTeC LD to iRacing

~~~powershell
ghost-car convert ld-to-iracing telemetry.ld `
  --target-profile target-profile.json `
  -o output.blap
~~~

When a companion `.ldx` exists, automatic lap selection uses its lap boundaries
and timing. Otherwise, the converter filters GPS outliers and selects the
fastest complete numbered lap. Use `--lap`, `--lap-selection`, `--ldx`, or
`--ignore-companion-ldx` to control selection.

The target template or profile supplies the target-specific header, sectors,
distance bins, binary prefix, and reference spline. By default, the converter
fits the LD path to that spline and writes the resulting signed lateral offset.
When IBT supplies longitudinal correspondence, that map and the spline-derived
lateral map are joined by source distance, their only shared coordinate.
`--lateral-offset-source template` reuses the official lap's line, while
`--lateral-offset-source zero` follows the iRacing reference spline. Alignment
residual limits prevent a bad lap or coordinate mapping from silently producing
a file.

The start/end sector boundary floats are inferred vertical offsets. Their
track-specific cross-slope is learned from the target profile and applied to
the generated lap's first and last lateral offsets. Use
`--sector-boundary-source template` or `zero` for controlled alternatives.

Logs without latitude/longitude can use Cartesian positions by mapping the
internal horizontal `x`/`y` axes and optional vertical `z` axis explicitly:

~~~powershell
ghost-car convert ld-to-iracing telemetry.ld `
  --template target-track.blap `
  --channel "x=Car Coord X" `
  --channel "y=Car Coord Y" `
  --channel "z=Car Coord Z" `
  -o output.blap
~~~

Map the two horizontal axes to `x` and `y` and the vertical axis to `z`.
Coordinate names are logger-specific; a wrong mapping can still form a closed
lap, so the target-spline residual gate is authoritative.

When an iRacing `.ibt` recording of the target layout is available, its GPS
path can improve the longitudinal distance correspondence. The BLAP-derived
track spline still defines lateral offset and yaw coordinates:

~~~powershell
ghost-car convert ld-to-iracing telemetry.ld `
  --template target-track.blap `
  --reference-ibt target-track.ibt `
  --channel "x=Car Coord X" `
  --channel "y=Car Coord Y" `
  --channel "z=Car Coord Z" `
  -o aligned.blap
~~~

Use `--ibt-lap`, `--ibt-lap-selection`, and the `--alignment-*` / `--icp-*`
options to control reference-lap selection and fitting. Absolute GPS is used
only for longitudinal correspondence. BLAP/OLAP does not store absolute XYZ,
but it does store signed lateral position relative to an internal track spline.

## Package layout

| Path | Responsibility |
| --- | --- |
| `ghost_car/cli.py` | CLI parsing, file I/O, and command dispatch |
| `ghost_car/iracing.py` | Lossless iRacing BLAP/OLAP codec |
| `ghost_car/conversion.py` | Resampling, smoothing, pose, and control conversion |
| `ghost_car/ibt.py` | Native IBT GPS parsing and least-squares/ICP path alignment |
| `ghost_car/motec.py` | MoTeC loading, channel mapping, GPS cleanup, and lap selection |
| `ghost_car/__main__.py` | `python -m ghost_car` entry point |
| `ldparser/` | MoTeC parser Git submodule |
| `docs/` | Reverse-engineering notes and support boundaries |
| `pyproject.toml` | Package metadata and console entry point |
