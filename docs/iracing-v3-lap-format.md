# iRacing v3 BLAP/OLAP format notes

These observations are based on 19 BLAP/OLAP files from five layouts, two cars,
two drivers, and 31,797 samples. Constant fields are structural observations,
not proof of their private iRacing names.

## Binary layout

~~~text
0x0000..0x05AF  1456-byte metadata header
0x05B0..0x05BF  16-byte table header
0x05C0..         N x 32-byte sector records
body             M x 28-byte distance-grid samples
~~~

Sector records use little-endian `<ffIffffI>`: start distance, end distance,
bin count, sample spacing, two unresolved boundary floats, sector best time,
and record flags.

Samples use little-endian `<ffffffI>`: time within sector, signed lateral
offset in metres, yaw, pitch, roll, reserved float, and packed flags. The flag
bytes, most significant first, are gear, clutch raw, brake raw, and throttle
raw. The lapfile clutch byte is always `0xFF`. In the matching MX-5 IBT,
`ClutchRaw` is always `1.0`, which quantizes exactly to 255, while no
Wing/Aero/Flap/DRS channel exists. This makes clutch raw substantially better
supported than a wing-angle interpretation. The reserved float is always
`0.0`.

## Lateral-offset evidence

Treating sample float 2 as `BLAP time - OLAP time` gives only `R² 0.000–0.141`.
Across comparable official pairs, the track-coordinate relationship

~~~text
yaw - splineYaw ~= atan(d(lateralOffset) / ds)
~~~

gives correlation `0.788–0.949`, `R² 0.621–0.901`, and fitted small-angle
slopes `0.882–1.048`. The value is continuous across every observed sector
boundary. This is why the canonical name is `lateralOffsetM`, not `deltaS`.

The sign is defined by the inferred spline's left normal. The fitted rotation
also converts source yaw into the BLAP coordinate frame.

A spline inferred from one driven lap retains some body-yaw and tire-slip bias.
Profiles can therefore average inferred splines from multiple official
BLAP/OLAP laps of the exact same layout. The primary lap remains the sole source
of packed metadata and sectors. When an IBT path supplies longitudinal mapping,
its distance map and the BLAP-spline lateral map are combined by source
distance; target distance is not a valid join key between independently fitted
maps.

For a closed lap, distance-map correction must also be continuous at the
start/finish boundary. Anchoring only the final sample to zero creates an
artificial local distance stretch and therefore a speed spike because BLAP
speed is inferred from `ds/dt`. ghost-car removes the smoothed correction's
endpoint trend across the lap, then preserves exact sector durations while
smoothing positive time increments. Tangent yaw is reconstructed from the
inferred reference spline plus the generated lateral-offset gradient, matching
the path iRacing actually renders instead of the pre-projection source GPS
tangent. Both output and input headings use circular smoothing and shortest-arc
interpolation across the 0/360-degree boundary.
Time-increment smoothing uses reflected sector boundaries by default so the
polynomial filter cannot extrapolate a false finish-line speed; the legacy
truncated boundary fit remains available through the advanced CLI.
Generated tangent pitch negates the positive-up source slope because the
lapfile convention is positive for nose-down rotation.

## Metadata confidence

| Field | Observation | Confidence |
| --- | --- | --- |
| build date 1 | varies with the car resource | high |
| build date 2 | shared simulator/data dependency | medium |
| build date 3 | varies with track/layout resource | high |
| table header word 0 | always zero | structural only |
| table version candidate | always three and matches header version in every v3 file | plausible mirrored format version; needs a non-v3 sample |
| record flags | always `0x3` | unresolved bit meanings |
| sector boundary floats | lateral offset times a track-specific cross-slope, per-layout R² 0.9992–1.0000 | inferred vertical boundary offset |
| header flags | always zero | unresolved feature mask |

The opaque header visibly contains repeated driver/car snapshots and design
strings. Their exact packed schema is not fully decoded, so ghost-car preserves
the verified prefix in a target profile.

## Generation boundary

An LD file alone cannot identify the target iRacing resource versions, sector
grid, packed metadata, or internal reference spline. A legal conversion must
receive either a valid BLAP/OLAP for the exact target car and layout or a target
profile previously extracted from one. IBT GPS can improve longitudinal
alignment, but it records a driven path rather than the private reference
spline or complete lapfile prefix.

The two sector boundary floats are generated from the output lap's first and
last `lateralOffsetM` and a start/finish cross-slope fitted from the target
profile. `--sector-boundary-source template` preserves the original values and
`zero` is available for controlled experiments.
