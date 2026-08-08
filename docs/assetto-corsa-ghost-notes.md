# Assetto Corsa ghost format notes

## Status

Assetto Corsa ghost decoding, generation, conversion, and installation are not
supported by this project. This document preserves observations from black-box
experiments. It is research documentation, not a format
specification or compatibility promise.

## Confirmed observations

The following statements were reproduced across multiple locally recorded
version-4 files unless a narrower scope is stated:

- The legacy core starts with little-endian version and lap-count integers,
  length-prefixed UTF-8 strings for driver, track, layout, and car, followed by
  lap time in milliseconds and frame count.
- Each observed core frame contained nine 4x4 matrices of little-endian
  32-bit floats: 576 bytes per frame. Matrix count should not be assumed to be
  universal.
- A core file can be decoded and re-encoded byte-for-byte when all header,
  frame, and trailing bytes are preserved.
- CSP multi-ghost files append records beginning with the 12-byte marker
  `00 01 02 03 2E 63 73 70 5F 65 78 74`, followed by a one-byte record type,
  a little-endian payload length, and the payload.
- Six CSP record types, numbered 1 through 6, were observed.
- Observed compressed records store a little-endian uncompressed length and a
  raw DEFLATE stream.
- In the sampled files, the uncompressed type-6 payload was exactly 12 bytes
  per core frame.
- In the sampled files, the type-3 payload contained three little-endian
  sector times whose sum equaled the header lap time.
- Two other payload lengths remained fixed across recordings from the same
  car, track, layout, and tyre combination. Their semantics were not proven.
- In one traced CSP workflow, the runtime `.tmp` file was byte-identical to
  the core prefix of the selected CSP multi-ghost file; the CSP extension was
  omitted from `.tmp`.
- File-I/O tracing showed CSP reading a layout-specific multi-ghost path before
  the legacy path. Removing the selected multi-ghost removed the visible ghost.
- Installing only a legacy `.ghost`, only a matching `.tmp`, or both did not
  produce a visible ghost in the tested CSP configuration, even when using a
  previously visible native core.
- Native recordings in the sampled environment used a cadence near 7.75 Hz.
  A generated 60 Hz core was rejected, but matching the native cadence was not
  sufficient to make a generated legacy core visible.

## Not established

- The meanings and versioning rules of every CSP extension record
- Whether CSP validates checksums or relationships not visible in the core
- How extension records vary by CSP version, car scripts, physics extensions,
  tyre compound, track, or session configuration
- A template-free method for constructing all required extension payloads
- A stable installation contract across vanilla Assetto Corsa, Content
  Manager, and CSP versions

## Why support was removed

Parsing the legacy core is not equivalent to producing a loadable ghost. In the
tested environment, runtime selection depended on an undocumented CSP container
whose per-frame and per-session records were only partially understood. Files
that were structurally valid and round-tripped exactly could still be silently
ignored by the game.

Shipping conversion or installation commands under those conditions would
misrepresent experimental output as supported functionality and could overwrite
valid files. There is also no automated acceptance test for the
actual game runtime. Support should only be reconsidered with a documented or
fully characterized CSP extension format, fixtures covering multiple versions
and content combinations, and an end-to-end runtime validation strategy.
