# Assetto Corsa .acreplay format notes

Status: the version-16 core layout and the CSP extension container are
documented below and verified against locally recorded CSP replays. This
document is research documentation. Parsing and template-based LD conversion
are supported by `ghost_car.acreplay` and `ghost_car.replay_writer`.

## Verified sample set

All observations were reproduced across 31 locally recorded version-16 files:
9 single-car circuit replays (198-210 track objects, up to 88,599 frames,
241 MB) and 22 replays across multiple circuit/road/custom layouts, including
4-car race replays (0 track objects, 2,438 frames). All files carry the CSP
container footer; vanilla (non-CSP) files were not sampled.

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
XYZ. Status bit field (little endian), as decoded by the community:

~~~text
bit 0..1 unused; bit 2 unknown; bit 3 horn; bits 4..5 camera direction
(0 forward, 1 left, 2 right, 3 back); bits 6..7 unknown; bit 8 gearbox
being damaged; bit 9 unknown; bit 10 unknown; bit 11 lights; bits 12..15 unused
~~~

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
| load, damage, fuel, status bits | retained/resampled from the template |

The main risk is not byte layout but acceptance: the runtime may ignore
structurally valid files, as documented for the ghost format in
`assetto-corsa-ghost-notes.md`. A generated replay must therefore be
validated in the actual game before any conversion command is shipped.
Other open questions: whether track-object data must be plausible,
whether the EXT_PERFRAME/PERRACEFRAME streams are validated against the
core frames, and how extension records vary across CSP versions.

## Template-based writer

`ghost_car/replay_writer.py` implements a template-based writer in two
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

`ghost_car/ld_replay.py` provides the formal LD conversion pipeline and the
`ghost-car replay convert` command. GPS elevation is not assumed to share the
AC world-height datum. The default track-height mode projects each converted
X/Z sample onto the nearest segment of the AC reference path and uses that
interpolated Y. On the validation lap, the uncorrected vertical
RMSE relative to the AC reference was 1.16 m, with local errors from about
-1.9 m to +2.7 m; a constant offset did not materially reduce that error.

The body pose cannot be patched alone: both 12-float wheel-position blocks
contain AC-world coordinates. Leaving them unchanged put the generated body's
wheels hundreds of metres away on the template trajectory. Replaying every
template suspension sample also compressed a long unrelated drive into the
136-second generated lap, causing 10--15 mm P95 and 43--73 mm maximum wheel
height steps per frame. The writer now takes each wheel centre's median
body-local template position and transforms it with the replacement body's
full yaw, pitch, and roll. On the validation output the maximum local-position
error is 1.62 mm and the relative-height step is 1.22 mm at P95, 3.61 mm
maximum; the remaining error is consistent with replay angle quantization.

The tested LD file contains zero pitch and roll throughout, although the
aligned track grade spans about -3.4 to +3.5 degrees. Body pitch is therefore
derived from the smoothed tangent of the height-aligned 3D path. Validation
pitch/grade correlation is 0.9978 with 0.138-degree RMSE. Roll remains the LD
value and is zero for this source.

The two YXZ wheel-rotation blocks are world-space too. Native GR86 data shows
rear static-wheel yaw tracking body yaw and front static-wheel yaw adding a
same-sign road-wheel angle. The writer robustly calibrates the two front
road-wheel/steering-wheel scales from the same-car template, applies the new
body yaw and steering angle to both static and rolling rotations, and preserves
the rolling rotation's offset. The native and generated calibration factors
are approximately 0.078 and 0.077, respectively. A separate visual multiplier
can therefore make low-amplitude LD steering visible without corrupting that
calibration or the stored steering field. The 2.0 validation output reaches
8.76--8.79 degrees at P95 and 13.69--13.71 degrees maximum, matching the native
example's approximately 13.4--13.9-degree on-track P99 range. Rear-to-body yaw
error remains 0 degrees. Because LD does not provide compatible per-wheel
slip, the target car's slip angle, slip ratio, and nd-slip are zeroed to prevent
unrelated template skid smoke.

Component-wise wheel Euler interpolation is invalid even though each component
is numeric. A rolling wheel repeatedly changes YXZ representation at gimbal
singularities. On the first generated output, the dynamic-vs-static wheel-axis
P95 error was 116--118 degrees and the per-frame axle jump was about 116
degrees. Selecting complete native rotation triplets before applying the rigid
body/steering yaw reduced those values to 0.153--0.158 and 0.62--0.69 degrees,
respectively. The native template's dynamic-vs-static axle P95 error is about
0.145 degrees.

Raw 20 Hz GPS also produced visible sample-to-sample lateral wander, amplified
by interpolation onto the 15 ms replay grid. X/Z and heading now use a 0.75 s
zero-phase quadratic Savitzky-Golay filter before replay resampling. Isolated
heading glitches are rejected in unit-vector space before angle unwrapping.
The first and last windows are fitted separately: a single-lap replay is not
treated as a closed curve and different start/end lines are not blended.

### In-game validation results

Private validation artifacts were generated from single-car templates and
parse cleanly with the read-only parser. In-game results on an 8-car race
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

`ghost_car/ld_replay.py` converts a MoTeC LD lap into a morphed
replay: `extract_motec_points` supplies per-sample position, speed, rpm,
gear, accelerator pedal, brake, steering, pitch and roll; the LD east/north path
is rigidly fitted onto the template replay's driven x/z path (or, with
`--gps-track track.json`, mapped directly through a pre-calibrated
origin + ENU->AC matrix); the result is resampled to the 15 ms grid, yaw
is derived from the fitted path (smoothed, wrapped), and `morph` writes the
file. The pipeline produced structurally valid files from multiple private LD
samples (gear mapped iRacing -1/0/1.. -> AC 0/1/2..).

A same-layout validation used an ignored local LD/LDX lap mapped through an
ignored calibrated track reference. The output path matches the reference
with 3.29 m RMSE, completes exactly one yaw sweep, and loads and plays
correctly in-game. The template replay must be recorded on the exact same
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
