# Assetto Corsa .acreplay format notes

Status: the version-16 core layout and the CSP extension container are
documented below and verified against locally recorded CSP replays. This
document is research documentation. Parsing and template-based LD conversion
are supported by `ghost_car_generator.acreplay` and `ghost_car_generator.replay_writer`.

## Verified sample set

All observations were reproduced across a varied local set of version-16
single-car and multi-car circuit/road/custom-layout replays. All sampled files
carry the CSP container footer; vanilla (non-CSP) files were not sampled.

## File layout

~~~text
Header (fixed + length-prefixed UTF-8 strings, little-endian)
Global frame data:  numFrames x (4 + 12 x numTrackObjects) bytes
For each of numCars cars:
  CarHeader
  Frame 0:  20-byte frame header + 256-byte physics frame
  Frames 1..n-1:  numWings*4 wing bytes + frame header + physics frame
  numWings*4 wing bytes
  u32 trailingCount, then trailingCount x 8 bytes
CSP data section (when the CSP footer is present)
CSP footer
~~~

## Header

| Offset | Size | Type | Meaning |
| --- | --- | --- | --- |
| 0 | 4 | u32 | version, always 16 in samples |
| 4 | 8 | f64 | recording interval in ms (15.0 in samples) |
| 12 | var | lstring | weather / conditions name |
| var | var | lstring | track name |
| var | var | lstring | track config |
| var | 4 | u32 | number of cars |
| +4 | 4 | u32 | current recording index (equals frame count) |
| +8 | 4 | u32 | frame count |
| +12 | 4 | u32 | number of track objects |

`lstring` is a 4-byte little-endian length followed by that many UTF-8
bytes.

## Global frame data

4 bytes per frame (a 2-byte sun angle and 2 bytes of unresolved data)
plus 12 bytes per track object per frame. Track-object semantics were
not investigated; single-car replays recorded 198-210 objects and the
bulk of those files is this section.

## Car header

Five length-prefixed strings (car ID, driver name, nation code, driver
team, car skin ID) followed by u32 frame count and u32 wing count. A
race replay therefore records full session state: all cars, their
drivers, and AI levels.

## Frame header (20 bytes)

`<I4f`: timestamp in ms, ambient temperature, road temperature, wind
speed, wind direction. All values were 0 in the sampled skidpad race.

## Physics frame (256 bytes)

The exact little-endian layout, confirmed against a C++ community parser
and by decoding real files:

| Offset | Size | Type | Meaning |
| --- | --- | --- | --- |
| 0 | 12 | 3x f32 | body position X, Y (up), Z |
| 12 | 6 | 3x f16 | body rotation, stored YXZ, radians |
| 18 | 2 | - | padding |
| 20 | 48 | 12x f32 | wheel static positions (FL, FR, RL, RR x XYZ) |
| 68 | 24 | 12x f16 | wheel static rotations (stored YXZ) |
| 92 | 48 | 12x f32 | wheel positions |
| 140 | 24 | 12x f16 | wheel rotations (stored YXZ) |
| 164 | 6 | 3x f16 | velocity XYZ, m/s |
| 170 | 2 | f16 | rpm |
| 172 | 8 | 4x f16 | wheel angular velocity |
| 180 | 8 | 4x f16 | slip angle |
| 188 | 8 | 4x f16 | slip ratio |
| 196 | 8 | 4x f16 | nd slip |
| 204 | 8 | 4x f16 | wheel load, N |
| 212 | 2 | f16 | steering wheel angle, degrees |
| 214 | 2 | f16 | bodywork noise |
| 216 | 2 | f16 | drivetrain speed |
| 218 | 2 | - | padding |
| 220 | 12 | 3x u32 | current / last / best lap time, ms |
| 232 | 1 | u8 | fuel, 0-255 |
| 233 | 1 | u8 | fuel per lap, 0-255 |
| 234 | 1 | u8 | gear: 0 reverse, 1 neutral, 2 first |
| 235 | 4 | 4x u8 | tire dirt |
| 239 | 5 | 5x u8 | damage front deformation, rear, left, right, front |
| 244 | 2 | 2x u8 | gas, brake (0-255) |
| 246 | 2 | 2x u8 | current lap (0-based), unknown (usually 0) |
| 248 | 2 | u16 | status bits (lights, horn, camera direction, ...) |
| 250 | 2 | u16 | unknown2 |
| 252 | 3 | 3x u8 | dirt, engine health, boost |
| 255 | 1 | - | padding |

`f16` is IEEE-754 binary16. Rotations are stored YXZ; the parser returns
XYZ. The core status bit field is little endian. Community parsing plus the
sampled native replay corpus currently supports:

~~~text
bits 0..1 reserved; bit 2 session/control-mode flag (low confidence);
bit 3 horn; bits 4..5 camera direction (0 forward, 1 left, 2 right, 3 back);
bit 6 downshift request/process; bit 7 upshift request/process; bit 8 reserved;
bit 9 gearbox being damaged (community mapping, no local positive sample);
bit 10 unresolved persistent vehicle-state latch; bit 11 lap-boundary pulse;
bit 12 lights (community mapping, no local positive sample); bits 13..15 reserved
~~~

The bit-11 inference is strong rather than nominal: all observed positive
frames occur where currentLap changes or the current-lap timer
resets. Bits 6 and 7 form short runs around downshifts and upshifts,
respectively. Padding at offsets 18, 218, 247, and 250 remains zero in every
sampled native frame; offset 255 remains one and is treated as a frame-validity
sentinel. These observations are specific to the sampled version-16 CSP recordings
and do not prove behavior for every car or replay version.

Wing data is `numWings * 4` bytes between frames and after the last
frame; wing semantics were not investigated.

## CSP extension container

The footer is at the very end of the file:

~~~text
"__AC_SHADERS_PATCH_v1__"  (24 ASCII bytes)
u32 CSP data section offset
u32 container version, observed 1
~~~

Note: the low byte of the offset value is often 0x21 ('!'), which makes
hex dumps look like the marker has a trailing '!'. The marker is 24
bytes, not 25.

The data section begins with the session INI blob: several short
length-prefixed strings naming the weather implementation and
controller, then one length-prefixed string larger than 255 bytes
containing the session INI (`[BENCHMARK]`, `[WIND]`, and per-car
sections with `DRIVER_NAME`, `SKIN`, AI levels, etc.).

Extension records then follow, each:

~~~text
u32 name length
name bytes ("EXT_..." or "__AC_SHADERS...")
u32 payload length
payload
~~~

Observed records (payloads that start with a zlib header `78 01` are
DEFLATE-compressed):

| Record | Payload per frame (uncompressed / numFrames) | Notes |
| --- | --- | --- |
| EXT_PERRACE_v1 | - | 8 bytes, session ID (0x6A45AA00 in samples) |
| EXT_PERRACE_v2 | - | 16 bytes |
| EXT_SESSIONDATA_v1 | - | session name/type ("Quick Race", "START", ...) |
| EXT_CONDITIONSVERSION_v1 | - | 4 bytes, value 1 |
| EXT_PERFRAME_v1 | 56 B/frame | weather/conditions per frame |
| EXT_PERRACEFRAME_v1 | 16 B/frame | race-state per frame |
| EXT_PERCAR_v6:N | 108 B/frame | per-car extra state (clutch, handbrake, ...) |
| EXT_PERCAR_v7:N | 108 B/frame | per-car extra state, current CSP versions |
| EXT_EXTRASTREAM_v1 | - | chunks: u32 stream ID, u32 metadata, zlib data |

EXT_PERCAR versions 1-5 layouts are not documented (0 bytes per frame in
the community table). EXT_PERCAR v6/v7 field maps are decoded by the
community parser (see references): per-frame clutch, handbrake, wipers,
turn signals, low beams, ten extra option bits, and several unresolved
half/float/int fields.

EXT_EXTRASTREAM chunks carry an incrementing stream ID (e.g.
0xEB58FB85..0xEB58FB88) and a second u32 of unresolved meaning. The
sampled streams decompressed to all-zero dummy data.

## Existing implementations

- `abchouhan/acreplay-parser` (C++, GPL-3.0): full CarFrame and
  EXT_PERCAR v6/v7 decoding, CSV export for Blender. Source of the
  field maps used here.
- `inducer/acreplay` (Python, MIT): pure-Python parser for the core
  layout plus track plotting.
- `aiko-atami/acreplay-telemetry` (C++): telemetry export.

No open-source writer for .acreplay files was found.

## LD to replay feasibility

The replay stores full physics state, not inputs, at a fixed cadence
(66.7 Hz in samples). The LD -> acreplay mapping is:

| Replay field | Source |
| --- | --- |
| positionM | LD GPS X/Z after AC-world alignment; AC reference-path Y by default |
| rotationRad | yaw from heading/path; pitch and roll from LD channels |
| velocityMS | LD speed / Car Coord deltas |
| rpm, gear, brake, steerAngleDeg | corresponding LD channels |
| gas | LD driver accelerator/pedal channel, rest-calibrated; never implicit engine throttle position |
| current lap time | generated replay cadence |
| last/best lap time | retained/resampled from the template |
| wheel pose | template suspension/rolling pose transformed to new body/steering yaw |
| wing data, skin | template replay of the same car |
| track objects, weather strings | template replay of the same track |
| EXT_PERFRAME / PERRACEFRAME / EXTRASTREAM | resized template records |
| slip angle/ratio/nd-slip | zero: LD has no trustworthy per-wheel source |
| load, damage, fuel | retained/resampled from the template |
| status / unknown2 bitfields | nearest template frame; generated cars clear horn and rebuild the bit-11 lap pulse |

The main risk is not byte layout but acceptance: the runtime may ignore
structurally valid files, as documented for the ghost format in
`assetto-corsa-ghost-notes.md`. A generated replay must therefore be
validated in the actual game before any conversion command is shipped.
Other open questions: whether track-object data must be plausible,
whether the EXT_PERFRAME/PERRACEFRAME streams are validated against the
core frames, and how extension records vary across CSP versions.

## Template-based writer

`ghost_car_generator/replay_writer.py` implements a template-based writer in two
modes:

- `morph` patches the pose fields (position, rotation, velocity, rpm,
  gear, gas, brake, steer angle, lap times) of one car's 256-byte
  frames in place and rebuilds the car section. Everything else - wheel
  geometry, wing data, the INI blob, every CSP extension record, and
  the footer - is preserved byte-for-byte. A diff against the template
  confirmed that 67,649 changed bytes are confined to the pose fields.
- `resample` rebuilds the whole file at a new frame count. Known float
  fields in EXT_PERCAR v6/v7 are interpolated field-wise. Each static and
  dynamic wheel-rotation block is selected as one complete nearest native YXZ
  pose because its Euler components cannot be interpolated independently.
  Opaque global
  frame records and the incompletely
  documented mixed-layout EXT_PERFRAME and EXT_PERRACEFRAME records are
  selected by nearest frame so every source record remains byte-valid.
  EXTRASTREAM is resized to `ceil(frames / 16)`; the session data table is
  copied verbatim and the footer offset is rewritten.
- `replicate_car` turns a single-car template into up to 16 comparison cars.
  It clones the complete car frame section, rebuilds the native 20-byte-per-car
  session table, creates `[CAR_N]` INI sections, updates `[RACE] CARS`, and
  emits one indexed EXT_PERCAR plus one incrementing-ID EXTRASTREAM per car.

`ghost_car_generator/ld_replay.py` provides the formal LD conversion pipeline and the
`ghost-car replay convert` command. GPS elevation is not assumed to share the
AC world-height datum. The default track-height mode projects each converted
X/Z sample onto the nearest segment of the AC reference path and uses that
interpolated Y. Local validation showed that the vertical error changes around
the lap, so a single constant GPS-altitude offset is insufficient.

The body pose cannot be patched alone: both 12-float wheel-position blocks
contain AC-world coordinates. Leaving them unchanged put the generated body's
wheels hundreds of metres away on the template trajectory. Replaying every
template suspension sample also compressed a long unrelated drive into the
generated lap, causing visible wheel-height steps. The writer now takes each
wheel centre's median
body-local template position and transforms it with the replacement body's
full yaw, pitch, and roll. The remaining local-position error and height steps
are consistent with replay angle quantization.

Some LD exports contain no useful pitch signal. Body pitch is therefore
derived from the smoothed tangent of the height-aligned 3D path, which closely
tracks the reconstructed grade in local validation. Roll remains the LD value.

The two YXZ wheel-rotation blocks are world-space too. Native same-car data shows
rear static-wheel yaw tracking body yaw and front static-wheel yaw adding a
same-sign road-wheel angle. The writer robustly calibrates the two front
road-wheel/steering-wheel scales from the same-car template, applies the new
body yaw and steering angle to both static and rolling rotations, and preserves
the rolling rotation's offset. A separate visual multiplier can therefore
make low-amplitude LD steering visible without corrupting that calibration or
the stored steering field. Rear wheels continue to track body yaw. Because LD
does not provide compatible per-wheel
slip, the target car's slip angle, slip ratio, and nd-slip are zeroed to prevent
unrelated template skid smoke.

Component-wise wheel Euler interpolation is invalid even though each component
is numeric. A rolling wheel repeatedly changes YXZ representation at gimbal
singularities. Selecting complete native rotation triplets before applying the
rigid body/steering yaw removes the extreme axle-axis discontinuities and
brings the generated error in line with the native template.

Raw 20 Hz GPS also produced visible sample-to-sample lateral wander, amplified
by interpolation onto the 15 ms replay grid. X/Z and heading now use a 0.75 s
zero-phase quadratic Savitzky-Golay filter before replay resampling. Isolated
heading glitches are rejected in unit-vector space before angle unwrapping.
The first and last windows are fitted separately: a single-lap replay is not
treated as a closed curve and different start/end lines are not blended.

### In-game validation results

Private validation artifacts were generated from single-car templates and
parse cleanly with the read-only parser. In-game results on a multi-car race
replay:

- Position changes are applied; the game plays a morphed car around an
  80 m circle while the untouched AI cars run their recorded lap.
- The body rotation field (offset 12, three binary16) is applied only
  for yaw values wrapped into [-pi, pi]. A yaw sweep to 21 rad was
  silently ignored; after wrapping to [-pi, pi] the car turned
  correctly. Multi-lap native recordings store the same wrapped range
  (observed -3.140625..3.140625 in a multi-turn replay).
- The yaw convention is forward = (-sin(yaw), cos(yaw)) in the world
  x/z plane. Writing the naive (+pi/2) tangential convention made the
  car point at the circle centre; removing the offset fixed it.
- Frame-header timestamps must be 0; every native recording sampled
  stores 0 there, and the first resampled files wrote i*15 ms and were
  rejected. The frame's trailing byte (offset 255) is a constant flag
  (observed 1) and must be preserved.
- A generated single-car replay on a matching circuit layout with LD-derived
  poses loads and plays in-game end to end; the car drives the recorded GPS
  line with wrapped yaw, pedals, rpm, and gear from the LD channels.

### LD to replay pipeline

`ghost_car_generator/ld_replay.py` converts a MoTeC LD lap into a morphed
replay: `extract_motec_points` supplies per-sample position, speed, rpm,
gear, accelerator pedal, brake, steering, pitch and roll; the LD east/north path
is rigidly fitted onto the template replay's driven x/z path (or, with
`--gps-track track.json`, mapped directly through a pre-calibrated
origin + ENU->AC matrix); the result is resampled to the 15 ms grid, yaw
is derived from the fitted path (smoothed, wrapped), and `morph` writes the
file. The pipeline produced structurally valid files from multiple private LD
samples (gear mapped iRacing -1/0/1.. -> AC 0/1/2..).

The converter also supports a full-session mode and two multi-car comparison
timings. Full-session timing comes from the monotonic LD running-time channel;
the 0-based AC current-lap field and current-lap timer are reset when the LD
lap channel changes. The default actual-time comparison preserves each lap's
recorded timing, holds completed cars at their final pose, and runs until the
slowest selected lap finishes. Progress comparison instead retimes each path
to the fastest selected duration using shared track station, keeping cars at
equivalent progress for a line-only view. Neither mode forces an endpoint to
meet the other. Comparison requires the shared GPS-to-AC track calibration so
per-lap rigid fitting cannot absorb line differences. Selected-lap local GPS
coordinates are translated from their lap origin to the shared track origin
before applying the ENU-to-AC matrix.

A track JSON's horizontal `enuToAc.matrix` is authoritative when supplied.
`replay offset-track` can make a measured shared X/Z translation correction by
changing only matrix elements `[0][3]` and `[2][3]` in a new file. It does not
move `referencePathAc`, alter the fit rotation/reflection, or independently
align laps. The cumulative manual correction is stored in
`calibration.manualOffsetAcM` for auditability and reversal.

`replay calibrate-track` formalizes the trajectory-shape calibration. Its AC
reference can be a native full-lap replay, a track JSON, or a package directory.
One or more selected LD laps are moved into a shared geodetic ENU frame,
represented as closed paths, resampled to equal-distance stations, and combined
with a pointwise median. The fitter searches circular phase and forward/reversed
correspondence, solving rotation or reflection plus translation for every
candidate. Scale is fixed at one unless explicitly enabled and bounded.

The output stores the winning phase/reflection, ordered residual, aggregate and
per-source nearest-path errors, and source/target lengths. It deliberately omits
source filenames, hashes, selected lap numbers, and lap times, and never fits
separate per-lap
transforms. AC height and the 3D reference path come from the native AC
reference; GPS altitude remains a separate conversion concern.

A track package consists of a manifest, calibration JSON, and optional
`.gcsurface` files in one directory. Both legacy `package.json`/`track.json` and
bundled `manifest.json`/`calibration.json` names are supported. `--gps-track`
accepts a directory, JSON, or `builtin:` resource. Package surfaces are resolved
relative to the manifest and loaded automatically for KN5 height mode. The
repository's KN5 extractor directly
parses KN5 v1-v6 node geometry in Python, skips textures/materials, applies base
node transforms, filters renderable meshes by regex, and writes `GCSURF1`.

A same-layout validation used ignored local inputs. The output path stayed
within the configured reference tolerance, completed one yaw sweep, and loaded
and played correctly in-game. The template replay must be recorded on the exact same
track layout as the LD lap.

### Resample crash cause (session data table between cars and CSP section)

Changing the frame count with the first resample version crashed the
game with an access violation. Minidump analysis (x86_64 context walk,
module RVA 0x1585BB in acs.exe) pinned the fault to
`movzx ecx, byte ptr [rdi + 0x1000]` with `rdi = NULL`. The NULL came
from a `std::vector<T*>::at(index)` (RVA 0x1992E0, returns NULL when
the index is out of bounds), where the index is a u32 read from the
file. The loader reads it from a session data table that sits between
the last car and the CSP INI blob:

~~~text
u32 groupCount
groupCount x numCars x 20 bytes:  [u32 typeIndex][u32 x4]
~~~

Verified on native replays: 6,131-frame (groupCount=1, 24 bytes),
28,173-frame (groupCount=3, 64 bytes), 4-car (groupCount=1, 84 bytes).
The resample writer initially dropped this region, so the loader read
the INI blob length as groupCount and garbage type indices, crashed the
vector lookup, and dereferenced NULL. Resample now copies the region
verbatim and the CSP footer offset points past it.

The same crash could in principle be triggered by any corrupted
20-byte entry; the type index is looked up in a handler table and
`[index*8]` is dereferenced (+0x1000) without a bounds check on the
result.

Additionally: EXT_EXTRASTREAM_v1 payloads decompress to exactly
`ceil(numFrames / 16)` bytes (verified on 2,438-frame (153 B),
6,131-frame (384 B), and 28,173-frame (1,761 B) native replays), and
CSP indexes that stream by frame, so a stale length reads out of
bounds. Resample rebuilds the chunk to the correct length (zero content
is valid; the sampled native streams are all zeros). EXT_PERCAR streams
are rebuilt field-wise (f32/f16 interpolated, integer/byte fields held
at their first-frame values), weather streams are rebuilt as float32
columns, and yaw is unwrapped at the LD's native cadence before
interpolation to the replay grid: interpolating the wrapped angle
across its +-pi seam swings the signal through zero and destroys the
lap sweep.
