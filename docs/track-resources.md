# Bundled track resources

Resources live under `src/ghost_car_generator/resources/tracks/<simulator>/` so they are
included in source distributions and wheels without mixing simulator schemas.

## Assetto Corsa calibration packages

Each `<track>/<layout>/` directory contains:

- `manifest.json`: track/layout identity, relative asset names, and KN5 extraction
  recipes.
- `calibration.json`: geodetic origin, ENU-to-AC transform, AC start line/reference
  path, and calibration diagnostics.
- locally generated `.gcsurface` files: only the mesh geometry selected for
  height matching; textures and materials are never included. These files are
  ignored by Git and are not distributed by this repository.

Pass a filesystem directory or its `builtin:<simulator>/<track>/<layout>` ID to
`ghost-car replay convert --gps-track`. In KN5 height mode, locally generated
surfaces listed by the manifest are loaded automatically; an explicit
`--track-surface` is recommended for an installed, read-only package.

Create surfaces with `ghost-car replay export-kn5-surface`. Create or update
`calibration.json` with `ghost-car replay calibrate-track`; do not independently
align each lap because that removes genuine racing-line differences.

Do not commit `.gcsurface` files derived from a third-party track mod. Commit the
manifest and calibration JSON only; users generate the surfaces locally from
their installed KN5 assets. Calibration output omits source filenames and file
hashes, selected lap numbers, or lap times by default.

## iRacing axis references

Each resource contains a sanitized, vehicle-independent `axis.json`. It is not
a BLAP/OLAP file or target profile and contains no private lap prefix, GPS
origin, vehicle/driver metadata, or source-file hashes.
