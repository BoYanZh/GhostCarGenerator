# LD → Assetto Corsa replay: handoff notes

Status: the end-to-end pipeline works in-game and is exposed as
`ghost-car replay convert`. A MoTeC LD lap can be converted into a playable
Assetto Corsa `.acreplay` that reproduces the recorded GPS line, wrapped yaw,
pedals, rpm, gear, track-derived pitch, stable wheel centres, and visible front
steering. The final in-game check confirmed the accepted 2.0x visual steering
scale, correct pitch direction, stable wheel height, aligned rear wheels, no
false skid smoke, and no wheel-axis/camber flashing.

This document is for a follow-up agent. Read
`docs/assetto-corsa-replay-notes.md` for the format facts and the crash
post-mortems, and `README.md` for the package overview.

## Current state (what works)

- `ghost_car/acreplay.py` - read-only parser: header, global frame
  data, per-car 256-byte physics frames, CSP extension records
  (decompression, per-frame sizes, EXTRASTREAM chunk ids).
- `ghost-car replay inspect <file>` - CLI JSON dump of the above.
- `ghost_car/motec.py` - `extract_motec_points` now also returns rpm
  and `steerRad` (aliases `rpm` / `steering`).
- `ghost_car/replay_writer.py` - template-based writer:
  - `morph` patches one car's pose fields in place (byte-for-byte
    faithful elsewhere);
  - `resample` rebuilds the file at a new frame count (car frames,
    global sun data, per-frame CSP streams, EXTRASTREAM length,
    session data table, footer offset).
- `ghost_car/ld_replay.py` - LD → replay:
  `extract_motec_points` → GPS transform (rigid fit, or
  `--gps-track track.json` ENU→AC matrix) → 15 ms resample → yaw from
  GPS heading/path → `morph`.
- `ghost-car replay convert` - formal CLI, including calibrated track
  transforms, track-layout validation, and three height modes.
- Tests: parser/writer, height/alignment, pose/control, and corridor coverage.

## Private validation inputs

- LD + LDX recording ignored by Git
- Calibrated track reference (origin + ENU→AC matrix + centerline), ignored by
  Git
- Native same-car, same-layout AC template replay outside the repository
- Reference conversion command:

~~~powershell
python -m ghost_car replay convert `
  native-template.acreplay `
  telemetry.ld `
  -o scratch\converted.acreplay `
  --gps-track private-track-reference.json --lap 2 `
  --wheel-steer-multiplier 2.0
~~~

Copy the output into the AC replay folder and play it. Validation checks the
track-reference tolerance, one-lap yaw sweep, and CSP stream lengths against
the frame count. Native replays,
telemetry, track references, and generated outputs must remain private.

## Hard-won facts (do not re-derive)

1. Frame-header timestamps must be 0 (all native files store 0).
2. Body yaw must be wrapped to [-pi, pi]; out-of-range yaw is silently
   ignored by the game. Native multi-lap recordings store the wrapped
   range too. Yaw convention: forward = (-sin(yaw), cos(yaw)).
3. Frame trailing byte (offset 255) is a constant flag (observed 1);
   preserve it.
4. EXTRASTREAM decompressed size = ceil(numFrames / 16) per car; CSP
   indexes it by frame (stale length = out-of-bounds crash).
5. Session data table between the last car and the CSP INI blob:
   `u32 groupCount` then `groupCount × numCars × 20 bytes`, each entry
   `[u32 typeIndex][u32 ×4]`. Dropping it makes the loader read the INI
   length as groupCount and crash on a garbage type index (NULL deref
   at `[rdi+0x1000]`). Copy it verbatim; the CSP footer offset points
   past it.
6. CSP footer: `__AC_SHADERS_PATCH_v1__` (24 bytes) + u32 offset + u32
   version(1). The offset field's low byte is often 0x21 ('!'), which
   looks like a 25-byte marker in hex dumps; it is not.
7. EXT_PERCAR streams should be rebuilt field-wise (f32/f16
   interpolated, integer/byte fields held at first-frame values); yaw
   must be unwrapped at the LD's native cadence before interpolation to
   the replay grid.
8. In-game validation is the only real acceptance gate; structurally
   valid files can still be rejected (see the crash history in the
   notes). Test changes in-game, not just by parsing.

## Completed formalization

### A. Formalize: CLI command + tests

Completed in `ghost_car/replay_writer.py`, `ghost_car/ld_replay.py`, and
`ghost-car replay convert`. Synthetic tests cover morph/resample, opaque
trailing-data preservation, mixed CSP record resizing, height modes, yaw
wrapping, and controls. A track JSON is checked against the template's track
and layout.

The default `--height-mode track` preserves GPS X/Z while taking Y from the
nearest AC reference-path segment, removing GPS/AC vertical-datum mismatch
relative to that reference. The
`gps-offset` and `gps` modes remain available for comparison.

Wheel world positions now use median same-car template offsets fixed in body
space, avoiding time-compressed suspension movement and keeping residual
movement within replay quantization. GPS X/Z and yaw use a 0.75 s zero-phase quadratic filter; lap
start and finish are fitted independently and are never treated as a closed
loop. Local validation confirmed that the filter reduces lateral jitter
without forcing the lap endpoints together.

When the LD pitch signal is unusable, pitch is derived from the smoothed
aligned-track tangent and follows the reconstructed grade. Roll remains the LD
value.

The replay `gas` byte is mapped from driver accelerator input, not electronic
throttle-plate position. Default aliases intentionally exclude `Throttle Pos`.
The converter detects a stable low pedal-sensor rest cluster, maps it through
a 2% dead zone to zero, interpolates short missing runs, and normalizes gas and
brake independently. Local validation confirmed that released-pedal frames are
restored without introducing isolated one-frame zero glitches.

Both YXZ wheel-rotation blocks are now updated too. The writer infers each
front wheel's road-wheel/steering-wheel yaw scale from the same-car template,
then applies replacement body yaw, calibrated toe, and the LD steering angle.
Native and generated steering scales agree in local validation. The optional
`--wheel-steer-multiplier` changes only rendered front-wheel yaw, not the
stored LD steering field. It can make low-amplitude steering visible while the
rear wheels continue to track body yaw. LD has no trustworthy per-wheel slip channels, so
slip angle, slip ratio, and nd-slip are explicitly zeroed; the old tested
output contained unrelated template values large enough to trigger continuous
smoke.

Wheel rotations must not use the generic numeric interpolation path. Native
rolling wheels repeatedly cross YXZ singularities; interpolating x/y/z
separately caused extreme dynamic/static axle disagreement. The resampler now
selects each 12-value static/dynamic rotation block from one nearest native
frame, then `morph` applies body/steering yaw to that complete pose. The
remaining axle error is consistent with the native template.

## Validation status and future work

### B. Visual quality pass — completed

- In-game validation confirmed that fixed body-local wheel centres remove
  vertical bouncing and track-derived body pitch has the correct direction.
- The optional front-wheel steering scale is visible and has the correct sign;
  tune it against a native same-car example.
- Rear wheels remain aligned with the body, false skid smoke is absent, and
  wheel-axis/camber flashing is eliminated.
- Rolling rotation is synthesized from vehicle speed and a same-car effective
  tire radius/direction calibration; confirm its rate in-game.
- Derive roll from a trustworthy bank/attitude source only if a later visual
  pass shows that zero roll is inadequate.
- Compare the generated frames against the same-lap ghost frame by
  frame (the ghost has the native body orientation and wheel
  positions).
- Confirm speed consistency: the velocity vector is written from the
  LD speed channel; the game may derive rendering speed from frame
  deltas instead.

### C. Multi-lap and extended features

- Convert consecutive laps from the LDX lap markers (extend the
  LDX lap-interval handling in `extract_motec_points`), producing a
  multi-lap replay.
- Write sector/lap times into the frame `currentLapTimeMs` fields from
  the LDX splits (currently a linear 15 ms ramp).
- Validate the non-GPS rigid-fit path (`--gps-track` absent) in-game
  with a matching-track template.
- Handle EXT_PERCAR versions 1-5 (currently 0 bytes/frame in the
  community table).

## Constraints and cautions

- Do not distribute native replays or template files (personal data / possibly
  licensed content). The privacy-reduced resources under
  `src/ghost_car/resources/tracks/` show the publishable derived form.
- The parser supports CSP version-16 files only; vanilla (non-CSP)
  replays were not sampled.
- Anything generated must be validated in the actual game before it is
  called supported; see the crash history for what that looks like.
