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
│   └── encode
└── convert
    └── ld-to-iracing
~~~

Every leaf command provides its complete option reference:

~~~powershell
ghost-car iracing decode --help
ghost-car iracing encode --help
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

## MoTeC LD to iRacing

~~~powershell
ghost-car convert ld-to-iracing telemetry.ld `
  --template target-track.blap `
  -o output.blap
~~~

When a companion `.ldx` exists, automatic lap selection uses its lap boundaries
and timing. Otherwise, the converter filters GPS outliers and selects the
fastest complete numbered lap. Use `--lap`, `--lap-selection`, `--ldx`, or
`--ignore-companion-ldx` to control selection.

The target template supplies track-specific header data, sectors, distance
bins, and the binary prefix. Pose sources, smoothing, metadata, controls,
flags, channel mappings, units, GPS cleanup, and lap selection are exposed as
CLI options.

## Package layout

| Path | Responsibility |
| --- | --- |
| `ghost_car/cli.py` | CLI parsing, file I/O, and command dispatch |
| `ghost_car/iracing.py` | Lossless iRacing BLAP/OLAP codec |
| `ghost_car/conversion.py` | Resampling, smoothing, pose, and control conversion |
| `ghost_car/motec.py` | MoTeC loading, channel mapping, GPS cleanup, and lap selection |
| `ghost_car/__main__.py` | `python -m ghost_car` entry point |
| `ldparser/` | MoTeC parser Git submodule |
| `docs/` | Reverse-engineering notes and support boundaries |
| `pyproject.toml` | Package metadata and console entry point |
