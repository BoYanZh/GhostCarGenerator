# Ghost Car

Ghost Car is a Python package and CLI for iRacing BLAP/OLAP files.
It supports lossless binary-to-JSON round trips and conversion of MoTeC LD
telemetry into an iRacing lap using a target-track BLAP/OLAP template.

Assetto Corsa ghost files are intentionally not supported. The verified format
observations and the engineering reasons for that decision are recorded in
[docs/assetto-corsa-ghost-notes.md](docs/assetto-corsa-ghost-notes.md).

## Requirements

- Python 3.8 or newer
- The bundled `ldparser` submodule for MoTeC LD input

~~~powershell
git submodule update --init
python -m pip install -e .
~~~

The installed `ghost-car` command and `python -m ghost_car` are equivalent.

## CLI

~~~text
ghost-car
├── convert
├── inspect
├── profile
│   └── create
└── advanced
    ├── convert
    └── encode
~~~

The common workflow stays at the top level. Format-engineering and tuning
controls are isolated under `advanced`:

~~~powershell
ghost-car convert --help
ghost-car inspect --help
ghost-car profile create --help
ghost-car advanced convert --help
ghost-car advanced encode --help
~~~

PowerShell uses the backtick character (ASCII 96) for line continuation. It must
be the final character on the line, with no trailing spaces or comments. The
examples below can also be entered on one line by removing each backtick and
line break.

## Lossless BLAP/OLAP conversion

~~~powershell
ghost-car inspect input.blap -o lap.json
ghost-car advanced encode lap.json -o rebuilt.blap
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
ghost-car profile create target.blap -o target-profile.json
~~~

Multiple official laps from the exact same layout can reduce the yaw/slip bias
of any single driven lap when inferring iRacing's private track spline:

~~~powershell
ghost-car profile create target.blap `
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
ghost-car convert telemetry.ld `
  --profile target-profile.json `
  -o output.blap
~~~

The same command on one line is:

~~~powershell
ghost-car convert telemetry.ld --profile target-profile.json -o output.blap
~~~

When a companion `.ldx` exists, automatic lap selection uses its lap boundaries
and timing. Otherwise, the converter filters GPS outliers and selects the
fastest complete numbered lap. Use `--lap` for a specific numbered lap.
Alternative selection and LDX controls are available through
`ghost-car advanced convert`.
Common GPS names and ACTI's `Car Coord X/Y/Z` channels are detected
automatically; unusual logger channel names can be mapped in the advanced CLI.

The target template or profile supplies the target-specific header, sectors,
distance bins, binary prefix, and reference spline. By default, the converter
fits the LD path to that spline and writes the resulting signed lateral offset.
When IBT supplies longitudinal correspondence, that map and the spline-derived
lateral map are joined by source distance, their only shared coordinate.
Closed-lap distance corrections are anchored continuously across start/finish
instead of forcing a discontinuous final sample. Cumulative sample time is
rebuilt from positive smoothed time increments while preserving every sector's
exact duration, preventing an artificial speed spike at the finish line.
The default reflected boundary mode prevents one-sided polynomial extrapolation
at sector edges; `--smoothing-boundary-mode truncate` preserves the legacy mode.
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
ghost-car advanced convert telemetry.ld `
  --template target-track.blap `
  --channel "x=Car Coord X" `
  --channel "y=Car Coord Y" `
  --channel "z=Car Coord Z" `
  -o output.blap
~~~

Map the two horizontal axes to `x` and `y` and the vertical axis to `z`.
Coordinate names are logger-specific; a wrong mapping can still form a closed
lap, so the target-spline residual gate is authoritative.

Generated tangent yaw follows the path iRacing actually renders: the inferred
track spline plus the generated signed lateral offset. Its distance-based
spatial smoothing window is controlled by `--yaw-smoothing-distance-m` (32 m by
default). This avoids steering opposite the rendered line when source-distance
alignment and lateral projection differ. Input heading interpolation and all
heading smoothing use circular angles, so a transition between 359 and 1
degrees passes through 0 degrees rather than rotating through 180 degrees.
Generated tangent pitch converts the positive-up source-coordinate slope to
iRacing's positive-nose-down lapfile convention.

When an iRacing `.ibt` recording of the target layout is available, its GPS
path can improve the longitudinal distance correspondence. The BLAP-derived
track spline still defines lateral offset and yaw coordinates:

~~~powershell
ghost-car advanced convert telemetry.ld `
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
