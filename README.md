# GhostCarGenerator

Ghost Car is a Python package and CLI for iRacing BLAP/OLAP and Assetto
Corsa replay interoperability. It supports lossless iRacing binary-to-JSON
round trips and template-based conversion of MoTeC LD telemetry.

Assetto Corsa ghost files are intentionally not supported. The verified format
observations and the engineering reasons for that decision are recorded in
[docs/assetto-corsa-ghost-notes.md](docs/assetto-corsa-ghost-notes.md).

Assetto Corsa `.acreplay` files support inspection and template-based MoTeC
LD conversion. A converted circuit lap has been validated in-game with the
recorded line, yaw, pedals, rpm, gear, body pitch, and wheel pose. The format
notes and acceptance limits are in
[docs/assetto-corsa-replay-notes.md](docs/assetto-corsa-replay-notes.md).

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
├── replay
│   ├── inspect
│   └── convert
├── profile
│   └── create
└── advanced
    ├── convert
    ├── profile
    └── constrain
~~~

The common workflow stays at the top level. Format-engineering and tuning
controls are isolated under `advanced`:

~~~powershell
ghost-car convert --help
ghost-car inspect --help
ghost-car replay inspect --help
ghost-car replay convert --help
ghost-car profile create --help
ghost-car advanced convert --help
ghost-car advanced profile --help
ghost-car advanced constrain --help
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

The low-level `advanced encode` compatibility command is intentionally hidden
from normal command help; invoke it explicitly only when rebuilding a canonical
JSON file.

The canonical JSON retains the opaque binary prefix by default, enabling an
exact round trip for observed iRacing v3 files. Use `--omit-prefix` when the
prefix is not needed; encoding such JSON requires `--template`.

The second sample float is a signed lateral offset in metres relative to the
iRacing track spline. It is exposed as `lateralOffsetM`; `deltaS` is accepted
only when reading JSON produced by ghost-car 0.1.0 or older.

## Assetto Corsa replay inspection and LD conversion

`replay inspect` decodes the version-16 `.acreplay` core and CSP extension
container:

~~~powershell
ghost-car replay inspect replay.acreplay -o replay.json --max-frames 1000
~~~

Use `--max-frames 0` to decode every frame and `--include-raw-frames` to keep
the original 256 bytes per frame. The format notes are in
[docs/assetto-corsa-replay-notes.md](docs/assetto-corsa-replay-notes.md).

`replay convert` needs a native replay recorded with the target car and exact
track layout. A calibrated track JSON is recommended because it preserves the
GPS line in AC world coordinates and verifies the layout:

~~~powershell
ghost-car replay convert native-template.acreplay telemetry.ld `
  --gps-track calibrated-track.json `
  --lap 2 `
  --wheel-steer-multiplier 2.0 `
  -o converted.acreplay
~~~

GPS altitude and Assetto Corsa world height do not share a reliable datum.
The default `--height-mode track` keeps GPS X/Z but takes Y from the nearest
segment of the calibrated AC reference path. This prevents a car from floating
above or clipping through the track when the GPS elevation profile differs.
`gps-offset` retains the GPS elevation shape while removing its median datum
offset; `gps` keeps the raw mapped height. Use `--height-offset-m` only for a
small final body-height correction. Generated files still require an in-game
acceptance check because CSP replay rules are partly reverse-engineered.

LD GPS X/Z and heading are filtered on their native sample grid before the
15 ms replay resample. The default 0.75 s zero-phase quadratic window removes
visible sample-to-sample lateral wander without joining the end of a lap back
to its start; override it with `--position-smoothing-s` when needed. Both
world-coordinate wheel-position blocks use the same-car template's median
body-local wheel centres and follow the replacement body's full yaw, pitch,
and roll. This intentionally fixes suspension travel instead of
time-compressing an unrelated template drive into the generated lap. Body
pitch follows the smoothed tangent of the height-aligned 3D path because some
LD exports contain no useful pitch channel. Wheel yaw is rebuilt from the
replacement body yaw and an automatically calibrated same-car template
steering ratio. `--wheel-steer-multiplier` optionally scales only the rendered
front-wheel yaw while leaving the recorded steering field unchanged; its
default is 1.0. A 2.0 value maps the tested LD lap's 6.9-degree peak to the
native GR86 example's approximately 13.4--13.9-degree on-track P99 range.
Unsupported per-wheel slip channels are cleared instead of replaying unrelated
template skids and smoke. When the template frame count changes, each wheel
rotation is selected as one complete native YXZ triplet; its three Euler
components are never interpolated independently across wheel roll
singularities.

The replay `gas` byte represents driver accelerator input. Automatic channel
selection therefore accepts accelerator/pedal names but does not silently use
an engine `Throttle Pos` channel. A stable non-zero pedal-sensor rest position
is calibrated to zero with a small dead zone, short missing runs are linearly
filled, and gas/brake are normalized independently into their 0--255 fields.
Use `--channel throttle=CHANNEL_NAME` only when an explicit non-standard pedal
channel is required.

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

For a more accurate absolute reference spline, record two laps with deliberately
different lines and save both the BLAP/OLAP and IBT from each lap:

~~~powershell
ghost-car profile create left.blap `
  --matched-ibt left.ibt `
  --matched-pair right.blap right.ibt `
  -o target-profile.json
~~~

Each BLAP/IBT pair must describe the same lap. The CLI selects the complete IBT
lap whose duration is closest to the BLAP best-lap time; use
`--matched-ibt-lap` if the primary IBT is ambiguous. The two lines do not need
to touch the track edges, but they should differ laterally by at least one metre
over a useful part of the lap. Record both pairs on the same layout and simulator
build, without moving or rewriting either source file.

Matched reconstruction projects every IBT GPS path into one shared WGS84 local
frame, then solves the common iRacing reference spline from the absolute paths
and their BLAP lateral offsets. The output profile records fit residuals,
available lateral separation, the shared GPS origin, and hashes of every source
file. Expert separation, smoothing, lap-time, and residual limits are available
through `ghost-car advanced profile`.

The profile replaces a live template during later conversions. LD vehicle,
venue, and driver strings are recorded as provenance but do not replace target
metadata unless an explicit header override is supplied.

### Public track-reference artifacts

The repository may publish a sanitized track-reference artifact separately from
a target profile. For example,
`track_references/lagunaseca-2026.axis.json` contains only a local closed
east/north axis, lap fraction, layout identifier, and track length. It contains
no BLAP prefix, vehicle metadata, GPS origin, source-file hashes, or Formula Vee
provenance, and is not itself an iRacing lapfile or target profile.

The axis is layout-specific rather than vehicle-specific. A target profile still
needs a valid target-car BLAP/OLAP prefix and should remain private unless its
redistribution is permitted. Even a sanitized derived axis should be published
only after checking the applicable simulator terms.

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

## Constraining a lap within track-edge boundaries

Real-world GPS laps can drift several metres sideways while keeping a
plausible heading. When left/right edge reference laps are available for the
same layout (for example, two deliberately wide laps recorded with a lighter
car), their signed lateral offsets define the drivable corridor. The corridor
command keeps a lap inside that corridor without distorting it:

~~~powershell
ghost-car advanced constrain telemetry.blap `
  --left left-edge.blap `
  --right right-edge.blap `
  -o constrained.blap
~~~

`--mode translate` (default) shifts the whole lap by one constant lateral
amount so the maximum excursion outside the corridor is minimized; the lap
shape, yaw, timing, and controls are preserved exactly. This matches the
typical GPS-drift error model: position drifts, heading does not.
`--mode clamp` clips per-bin excursions and smooths the correction instead;
use it only when the line shape itself needs local correction.
`--tolerance-m` widens the corridor on both sides (1 m by default) because
edge laps normally do not use the full curbs. All inputs must share the same
track identifier and sector grid.

## Keeping generated files recognizable by iRacing

The BLAP header records the target car, track layout, and content build
dates. A lap generated against one car's template is not necessarily
recognized by iRacing for a different car, even when the layout matches.
To keep a generated lap associated with a specific car, repack it with a
native lap file of that car as the template:

~~~powershell
ghost-car advanced constrain telemetry.blap `
  --left left-edge.blap `
  --right right-edge.blap `
  --template native-car.blap `
  -o constrained.blap
~~~

The output then reuses the native lap's binary prefix and header (car, track,
build dates, sector flags) while retaining the constrained lateral offsets.
The same applies when any other command produces a lap whose header does not
match the target vehicle.

## Package layout

| Path | Responsibility |
| --- | --- |
| `ghost_car/cli.py` | CLI parsing, file I/O, and command dispatch |
| `ghost_car/iracing.py` | Lossless iRacing BLAP/OLAP codec |
| `ghost_car/acreplay.py` | Assetto Corsa .acreplay parser |
| `ghost_car/replay_writer.py` | Template-preserving replay morph and resampling |
| `ghost_car/ld_replay.py` | MoTeC-to-replay alignment, height correction, and conversion |
| `ghost_car/conversion.py` | Resampling, smoothing, pose, and control conversion |
| `ghost_car/ibt.py` | Native IBT GPS parsing and least-squares/ICP path alignment |
| `ghost_car/corridor.py` | Corridor-constrained lateral-offset correction |
| `ghost_car/motec.py` | MoTeC loading, channel mapping, GPS cleanup, and lap selection |
| `ghost_car/__main__.py` | `python -m ghost_car` entry point |
| `ldparser/` | MoTeC parser Git submodule |
| `docs/` | Reverse-engineering notes and support boundaries |
| `pyproject.toml` | Package metadata and console entry point |

## Legal and usage notice

GhostCarGenerator is an independent interoperability and research project. It
is not affiliated with, authorized by, or endorsed by iRacing.com Motorsport
Simulations, LLC. iRacing and other product names and trademarks belong to
their respective owners.

Use this software only with files and content you have lawfully obtained and
only where permitted by applicable law and the agreements governing the
relevant software. You are responsible for your use of the tool and for
compliance with those agreements.

This repository does not distribute iRacing BLAP/OLAP files, target profiles,
opaque binary prefixes, vehicle or track assets, or other proprietary game
data. Do not publish or redistribute those materials. A target profile can
contain an opaque prefix copied from the source lap file and should be treated
as private local data.

The project does not bypass DRM or anti-cheat systems, inspect process memory,
intercept network traffic, modify the iRacing client, or install files into it.
It is not intended for cheating, competitive advantage, disruption, or use in
official competition. No warranty is provided, including any warranty that a
generated file will remain compatible with future software versions.
