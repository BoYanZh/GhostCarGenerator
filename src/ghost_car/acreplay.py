"""Read-only parser for Assetto Corsa .acreplay files.

This module decodes the version-16 replay layout and the Custom Shaders
Patch extension container. It is verified against locally recorded CSP
replays; see docs/assetto-corsa-replay-notes.md for the field map.
Template-based writing lives in ghost_car.replay_writer.
"""
from __future__ import annotations

__all__ = [
    "VERSION",
    "POSTFIX",
    "CAR_FRAME_SIZE",
    "FRAME_HEADER_SIZE",
    "parse_acreplay",
    "parse_header",
    "parse_car",
    "parse_car_frame",
    "find_csp_data_offset",
    "parse_csp_extensions",
    "decompress_payload",
]

import os
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

BinarySource = Union[str, os.PathLike, bytes, bytearray]

# Observed little-endian version of every sampled replay.
VERSION = 16
# Footer layout at end of file: 24-byte marker, u32 CSP data offset,
# u32 container version (1).  The offset's low byte often reads as '!',
# which makes the marker appear to have a trailing '!' in hex dumps.
POSTFIX = b"__AC_SHADERS_PATCH_v1__"
POSTFIX_TAIL_SIZE = 8  # offset + container version

# Per-frame sizes, all little-endian.
FRAME_HEADER_SIZE = 20  # timestamp(ms) + ambient/road/wind-speed/wind-dir floats
CAR_FRAME_SIZE = 256  # full physics snapshot, see CAR_FRAME
GLOBAL_FRAME_BASE_BYTES = 4  # sun angle and one unknown half, per frame
TRACK_OBJECT_BYTES = 12  # per animated track object per frame
CSP_TRAILING_ENTRY_BYTES = 8  # per trailing entry after the last car frame

# Known bytes per CSP per-car extension frame, indexed by version (1-based).
# A value of 0 means the layout is not yet documented.
EXT_CAR_BYTES_PER_FRAME = (0, 0, 0, 0, 0, 108, 108)

# 256-byte physics snapshot.  'e' is IEEE-754 binary16.
# Offsets: 000 position XYZ f32; 012 rotation YXZ f16 (radians); 020 wheel
# static positions (4x XYZ f32); 068 wheel static rotations (4x YXZ f16);
# 092 wheel positions; 140 wheel rotations; 164 velocity XYZ f16; 170 rpm;
# 172 wheel angular velocity; 180 slip angle; 188 slip ratio; 196 ndSlip;
# 204 load; 212 steer angle (deg); 214 bodywork noise; 216 drivetrain speed;
# 220 current/last/best lap times (ms); 232 fuel; 233 fuel per lap; 234 gear
# (0 reverse, 1 neutral, 2 first); 235 tire dirt x4; 239..243 damage fields;
# 244 gas; 245 brake; 246 current lap; 247 unknown; 248 status bits; 250
# unknown2; 252 dirt; 253 engine health; 254 boost; 255 padding.
CAR_FRAME = struct.Struct(
    "<"
    "3f"  # position XYZ
    "3e"  # rotation YXZ
    "2x"  # padding
    "12f"  # wheelStaticPosition
    "12e"  # wheelStaticRotation
    "12f"  # wheelPosition
    "12e"  # wheelRotation
    "3e"  # velocity XYZ
    "e"  # rpm
    "4e"  # wheelAngularVelocity
    "4e"  # slipAngle
    "4e"  # slipRatio
    "4e"  # ndSlip
    "4e"  # load
    "e"  # steerAngle
    "e"  # bodyworkNoise
    "e"  # drivetrainSpeed
    "2x"  # padding
    "3I"  # currentLapTime, lastLapTime, bestLapTime
    "3B"  # fuel, fuelPerLap, gear
    "4B"  # tireDirt
    "5B"  # damageFrontDeformation, damageRear, damageLeft, damageRight, damageFront
    "2B"  # gas, brake
    "2B"  # currentLap, unknown
    "H"  # status
    "H"  # unknown2
    "3B"  # dirt, engineHealth, boost
    "B"  # trailing byte (observed constant 1 in native recordings)
)
assert CAR_FRAME.size == CAR_FRAME_SIZE

FRAME_HEADER = struct.Struct("<I4f")


def read_bytes(source: BinarySource) -> bytes:
    if isinstance(source, (str, os.PathLike)):
        return Path(source).read_bytes()
    return bytes(source)


def _read_lstring(raw: bytes, offset: int) -> Tuple[str, int]:
    (length,) = struct.unpack_from("<I", raw, offset)
    end = offset + 4 + length
    if end > len(raw):
        raise ValueError(
            "Length-prefixed string of {} bytes overruns file at 0x{:X}".format(
                length, offset
            )
        )
    return raw[offset + 4:end].decode("utf-8", errors="replace"), end


def parse_header(
    raw: bytes, offset: int = 0
) -> Tuple[Dict[str, Any], int]:
    """Decode the replay header and return ``(header, offset_after)``."""
    (version,) = struct.unpack_from("<I", raw, offset)
    offset += 4
    (recording_interval_ms,) = struct.unpack_from("<d", raw, offset)
    offset += 8
    weather, offset = _read_lstring(raw, offset)
    track, offset = _read_lstring(raw, offset)
    track_config, offset = _read_lstring(raw, offset)
    (
        num_cars,
        current_recording_index,
        num_frames,
        num_track_objects,
    ) = struct.unpack_from("<IIII", raw, offset)
    offset += 16
    header = {
        "version": version,
        "recordingIntervalMs": recording_interval_ms,
        "weather": weather,
        "track": track,
        "trackConfig": track_config,
        "numCars": num_cars,
        "currentRecordingIndex": current_recording_index,
        "numFrames": num_frames,
        "numTrackObjects": num_track_objects,
    }
    return header, offset


def parse_car_frame(raw: bytes, offset: int) -> Dict[str, Any]:
    """Decode one 256-byte physics snapshot."""
    values = CAR_FRAME.unpack_from(raw, offset)
    (
        px, py, pz,
        ry, rx, rz,
        # wheelStaticPosition [FL, FR, RL, RR] xyz
        wsp0x, wsp0y, wsp0z, wsp1x, wsp1y, wsp1z,
        wsp2x, wsp2y, wsp2z, wsp3x, wsp3y, wsp3z,
        # wheelStaticRotation (stored YXZ)
        wsr0y, wsr0x, wsr0z, wsr1y, wsr1x, wsr1z,
        wsr2y, wsr2x, wsr2z, wsr3y, wsr3x, wsr3z,
        # wheelPosition
        wp0x, wp0y, wp0z, wp1x, wp1y, wp1z,
        wp2x, wp2y, wp2z, wp3x, wp3y, wp3z,
        # wheelRotation (stored YXZ)
        wr0y, wr0x, wr0z, wr1y, wr1x, wr1z,
        wr2y, wr2x, wr2z, wr3y, wr3x, wr3z,
        # velocity, rpm, wheel angular velocity, slip, load
        vx, vy, vz,
        rpm,
        wav0, wav1, wav2, wav3,
        sa0, sa1, sa2, sa3,
        sr0, sr1, sr2, sr3,
        ns0, ns1, ns2, ns3,
        ld0, ld1, ld2, ld3,
        steer_angle,
        bodywork_noise,
        drivetrain_speed,
        current_lap_time, last_lap_time, best_lap_time,
        fuel, fuel_per_lap, gear,
        td0, td1, td2, td3,
        dmg_front_deformation, dmg_rear, dmg_left, dmg_right, dmg_front,
        gas, brake,
        current_lap, unknown,
        status, unknown2,
        dirt, engine_health, boost,
        trailing_byte,
    ) = values
    return {
        "positionM": [px, py, pz],
        "rotationRad": [rx, ry, rz],  # reordered from stored YXZ
        "wheelStaticPositionM": [
            [wsp0x, wsp0y, wsp0z],
            [wsp1x, wsp1y, wsp1z],
            [wsp2x, wsp2y, wsp2z],
            [wsp3x, wsp3y, wsp3z],
        ],
        "wheelStaticRotationRad": [
            [wsr0x, wsr0y, wsr0z],
            [wsr1x, wsr1y, wsr1z],
            [wsr2x, wsr2y, wsr2z],
            [wsr3x, wsr3y, wsr3z],
        ],
        "wheelPositionM": [
            [wp0x, wp0y, wp0z],
            [wp1x, wp1y, wp1z],
            [wp2x, wp2y, wp2z],
            [wp3x, wp3y, wp3z],
        ],
        "wheelRotationRad": [
            [wr0x, wr0y, wr0z],
            [wr1x, wr1y, wr1z],
            [wr2x, wr2y, wr2z],
            [wr3x, wr3y, wr3z],
        ],
        "velocityMS": [vx, vy, vz],
        "rpm": rpm,
        "wheelAngularVelocityRads": [wav0, wav1, wav2, wav3],
        "slipAngleRad": [sa0, sa1, sa2, sa3],
        "slipRatio": [sr0, sr1, sr2, sr3],
        "ndSlip": [ns0, ns1, ns2, ns3],
        "loadN": [ld0, ld1, ld2, ld3],
        "steerAngleDeg": steer_angle,
        "bodyworkNoise": bodywork_noise,
        "drivetrainSpeed": drivetrain_speed,
        "currentLapTimeMs": current_lap_time,
        "lastLapTimeMs": last_lap_time,
        "bestLapTimeMs": best_lap_time,
        "fuel": fuel,
        "fuelPerLap": fuel_per_lap,
        "gear": gear,
        "tireDirt": [td0, td1, td2, td3],
        "damageFrontDeformation": dmg_front_deformation,
        "damageRear": dmg_rear,
        "damageLeft": dmg_left,
        "damageRight": dmg_right,
        "damageFront": dmg_front,
        "gas": gas,
        "brake": brake,
        "currentLap": current_lap,
        "unknown": unknown,
        "status": status,
        "unknown2": unknown2,
        "dirt": dirt,
        "engineHealth": engine_health,
        "boost": boost,
        "trailingByte": trailing_byte,
    }


def parse_car(
    raw: bytes,
    offset: int,
    max_frames: Optional[int] = None,
    include_raw_frames: bool = False,
) -> Tuple[Dict[str, Any], int]:
    """Decode one car's header, frames, and trailing entries.

    Returns ``(car, offset_after_car)``.
    """
    car_id, offset = _read_lstring(raw, offset)
    driver_name, offset = _read_lstring(raw, offset)
    nation_code, offset = _read_lstring(raw, offset)
    driver_team, offset = _read_lstring(raw, offset)
    car_skin_id, offset = _read_lstring(raw, offset)
    num_frames, num_wings = struct.unpack_from("<II", raw, offset)
    offset += 8
    wing_bytes = num_wings * 4
    frames: List[Dict[str, Any]] = []
    limit = num_frames if not max_frames else min(num_frames, max_frames)
    for index in range(num_frames):
        (
            timestamp_ms,
            ambient_temp,
            road_temp,
            wind_speed,
            wind_direction,
        ) = FRAME_HEADER.unpack_from(raw, offset)
        offset += FRAME_HEADER_SIZE
        frame = parse_car_frame(raw, offset)
        offset += CAR_FRAME_SIZE
        frame.update(
            {
                "timestampMs": timestamp_ms,
                "ambientTempC": ambient_temp,
                "roadTempC": road_temp,
                "windSpeedMS": wind_speed,
                "windDirectionDeg": wind_direction,
            }
        )
        if index < limit:
            if include_raw_frames:
                frame["rawHex"] = raw[
                    offset - CAR_FRAME_SIZE:offset
                ].hex()
            frames.append(frame)
        if index < num_frames - 1:
            offset += wing_bytes
    offset += wing_bytes
    (trailing_count,) = struct.unpack_from("<I", raw, offset)
    offset += 4
    if trailing_count > 0:
        trailing_end = offset + trailing_count * CSP_TRAILING_ENTRY_BYTES
        if trailing_end > len(raw):
            raise ValueError(
                "Car declares {} trailing entries overrunning file at 0x{:X}".format(
                    trailing_count, offset
                )
            )
        offset = trailing_end
    car = {
        "carID": car_id,
        "driverName": driver_name,
        "nationCode": nation_code,
        "driverTeam": driver_team,
        "carSkinID": car_skin_id,
        "numFrames": num_frames,
        "numWings": num_wings,
        "frames": frames,
        "frameCountDecoded": limit,
        "trailingCount": trailing_count,
    }
    return car, offset


def find_csp_data_offset(raw: bytes) -> Optional[int]:
    """Return the CSP data-section offset from the footer, if present."""
    index = raw.rfind(POSTFIX)
    if index < 0:
        return None
    if index + len(POSTFIX) + POSTFIX_TAIL_SIZE > len(raw):
        return None
    (offset,) = struct.unpack_from("<I", raw, index + len(POSTFIX))
    (version,) = struct.unpack_from(
        "<I", raw, index + len(POSTFIX) + 4
    )
    if version != 1:
        return None
    if offset < 0 or offset >= len(raw):
        return None
    return offset


def decompress_payload(payload: bytes) -> Optional[bytes]:
    """Best-effort zlib decompression; returns None when not compressed."""
    try:
        return zlib.decompress(payload)
    except zlib.error:
        return None


def _walk_lstrings(
    raw: bytes, offset: int
) -> Tuple[Optional[str], int]:
    """Skip length-prefixed strings; return the first one longer than 255.

    The CSP data section begins with small strings naming the weather
    implementation and controller, followed by the session INI blob whose
    length exceeds 255.  Returns ``(ini_text, offset_after_ini)``.
    """
    while offset + 4 <= len(raw):
        (length,) = struct.unpack_from("<I", raw, offset)
        if length > 255:
            return _read_lstring(raw, offset)
        offset += 4 + length
    return None, offset


def parse_csp_extensions(
    raw: bytes,
    offset: int,
    num_frames: int,
) -> Tuple[Dict[str, Any], int]:
    """Parse the CSP extension container.

    Returns ``(csp, offset_after_records)``.  ``csp`` contains the session
    INI text, the list of extension records (payloads decompressed when
    zlib), and the trailing EXTRASTREAM chunk summary.
    """
    ini_text, offset = _walk_lstrings(raw, offset)
    if ini_text is None:
        raise ValueError("No session INI blob found at CSP data offset")
    records: List[Dict[str, Any]] = []
    while offset + 8 <= len(raw):
        (name_length,) = struct.unpack_from("<I", raw, offset)
        if name_length <= 0 or name_length > 255:
            break
        name_bytes = raw[offset + 4:offset + 4 + name_length]
        if not (
            name_bytes.startswith(b"EXT_")
            or name_bytes.startswith(b"__AC_SHADERS")
        ):
            break
        name = name_bytes.decode("ascii", errors="replace")
        offset += 4 + name_length
        (payload_length,) = struct.unpack_from("<I", raw, offset)
        offset += 4
        payload = raw[offset:offset + payload_length]
        offset += payload_length
        if len(payload) != payload_length:
            raise ValueError(
                "Extension {} overruns file at 0x{:X}".format(name, offset)
            )
        record: Dict[str, Any] = {
            "name": name,
            "offset": offset - 4 - name_length - 4 - payload_length,
            "payloadLength": payload_length,
        }
        if name.startswith("EXT_EXTRASTREAM"):
            record["streamId"] = struct.unpack_from("<I", payload, 0)[0]
            record["streamMetadata"] = struct.unpack_from("<I", payload, 4)[0]
            record["payload"] = payload[8:]
        else:
            record["payload"] = payload
        decompressed = decompress_payload(record["payload"])
        if decompressed is not None:
            record["compressed"] = True
            record["decompressedLength"] = len(decompressed)
            if num_frames and len(decompressed) % num_frames == 0:
                record["bytesPerFrame"] = len(decompressed) // num_frames
        else:
            record["compressed"] = False
        records.append(record)
    return {"iniText": ini_text, "records": records}, offset


def parse_acreplay(
    source: BinarySource,
    max_frames: Optional[int] = None,
    include_raw_frames: bool = False,
) -> Dict[str, Any]:
    """Parse an .acreplay file into a dictionary.

    ``max_frames`` caps how many physics frames are decoded per car.
    ``include_raw_frames`` attaches the raw 256 bytes per decoded frame.
    """
    raw = read_bytes(source)
    header, offset = parse_header(raw)
    if header["version"] != VERSION:
        raise ValueError(
            "Unsupported replay version {}; only version {} is documented".format(
                header["version"], VERSION
            )
        )
    num_frames = header["numFrames"]
    num_track_objects = header["numTrackObjects"]
    global_frame_bytes = (
        GLOBAL_FRAME_BASE_BYTES + TRACK_OBJECT_BYTES * num_track_objects
    ) * num_frames
    if offset + global_frame_bytes > len(raw):
        raise ValueError("Global frame data overruns file at 0x{:X}".format(offset))
    offset += global_frame_bytes
    cars: List[Dict[str, Any]] = []
    for _ in range(header["numCars"]):
        car, offset = parse_car(raw, offset, max_frames, include_raw_frames)
        cars.append(car)
    csp_offset = find_csp_data_offset(raw)
    csp: Optional[Dict[str, Any]] = None
    if csp_offset is not None:
        csp, offset = parse_csp_extensions(raw, csp_offset, num_frames)
    return {
        "header": header,
        "globalFrameBytes": global_frame_bytes,
        "cars": cars,
        "csp": csp,
        "cspDataOffset": csp_offset,
        "fileSizeBytes": len(raw),
    }
