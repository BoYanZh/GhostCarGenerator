"""Template-based Assetto Corsa ``.acreplay`` writer.

Two operations, both starting from a real (template) replay:

1. ``morph``  - byte-level patch: overwrite the pose fields (position,
   rotation, velocity, rpm, gear, gas, brake, steer, lap times) of one
   car's 256-byte frames in place.  Every other byte of the template -
   wheels, wing data, INI blob, CSP extension streams, footer - is
   preserved exactly, so the frame count and the CSP container stay
   valid by construction.

2. ``resample`` - rebuild the whole file at a new frame count. Known
   numeric fields are interpolated; opaque global/CSP records are selected
   by nearest frame without changing their bytes. The session INI is copied,
   EXTRASTREAM is resized, and the footer offset is rewritten.

The writer deliberately requires a native replay template. Assetto Corsa
replay metadata and CSP extension records are not safely synthesizable from
telemetry alone, so all non-telemetry data is retained from that template.
"""

__all__ = [
    "locate",
    "morph",
    "pack_car_frame",
    "patch_frame",
    "resample",
]

import json
import math
import struct
import zlib
from pathlib import Path

import numpy as np

from .acreplay import (
    CAR_FRAME,
    CAR_FRAME_SIZE,
    FRAME_HEADER_SIZE,
    POSTFIX,
    find_csp_data_offset,
    parse_car_frame,
    parse_header,
)

GLOBAL_FRAME_BASE_BYTES = 4
TRACK_OBJECT_BYTES = 12
CSP_TRAILING_ENTRY_BYTES = 8
DEFAULT_WHEEL_STEER_PER_STEERING_RAD = 0.08


def _lstring_bytes(text):
    raw = text.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def locate(raw):
    """Walk the template and capture every byte range we need to rebuild."""
    header, offset = parse_header(raw)
    global_bytes = (
        GLOBAL_FRAME_BASE_BYTES + TRACK_OBJECT_BYTES * header["numTrackObjects"]
    ) * header["numFrames"]
    global_start = offset
    global_end = offset + global_bytes
    offset = global_end
    cars = []
    for _ in range(header["numCars"]):
        car_start = offset
        for _ in range(5):
            (n,) = struct.unpack_from("<I", raw, offset)
            offset += 4 + n
        num_frames, num_wings = struct.unpack_from("<II", raw, offset)
        offset += 8
        header_end = offset
        frames = []
        wings = []
        for index in range(num_frames):
            frame_header = raw[offset:offset + FRAME_HEADER_SIZE]
            offset += FRAME_HEADER_SIZE
            frame = raw[offset:offset + CAR_FRAME_SIZE]
            offset += CAR_FRAME_SIZE
            frames.append((frame_header, frame))
            if index < num_frames - 1:
                wings.append(raw[offset:offset + num_wings * 4])
                offset += num_wings * 4
        wings.append(raw[offset:offset + num_wings * 4])
        offset += num_wings * 4
        (trailing,) = struct.unpack_from("<I", raw, offset)
        trailing_start = offset
        offset += 4 + trailing * CSP_TRAILING_ENTRY_BYTES
        cars.append(
            {
                "start": car_start,
                "headerBytes": raw[car_start:header_end],
                "frames": frames,
                "wings": wings,
                "numFrames": num_frames,
                "numWings": num_wings,
                "trailing": trailing,
                "trailingBytes": raw[trailing_start:offset],
            }
        )
    footer = raw.rfind(POSTFIX)
    if footer < 0:
        raise ValueError("Template has no CSP footer; only CSP replays are supported")
    csp_offset = find_csp_data_offset(raw)
    if csp_offset is None:
        raise ValueError("Template CSP footer has an invalid data offset")
    return {
        "header": header,
        "headerBytes": raw[:global_start],
        "globalStart": global_start,
        "globalEnd": global_end,
        "globalBytes": raw[global_start:global_end],
        "cars": cars,
        "cspOffset": csp_offset,
        "footer": footer,
        "tailBytes": raw[footer:],
    }


def pack_header(header):
    out = bytearray()
    out += struct.pack("<Id", header["version"], header["recordingIntervalMs"])
    out += _lstring_bytes(header["weather"])
    out += _lstring_bytes(header["track"])
    out += _lstring_bytes(header["trackConfig"])
    out += struct.pack(
        "<IIII",
        header["numCars"],
        header["currentRecordingIndex"],
        header["numFrames"],
        header["numTrackObjects"],
    )
    return bytes(out)


def _wrap_pi(angle):
    """Wrap an angle into [-pi, pi]; the game records yaw in this range."""
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _ac_rotation_matrix(rotation):
    """Return the body-to-world matrix for AC's raw YXZ Euler angles."""
    pitch, yaw, roll = rotation
    cx, sx = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cz, sz = math.cos(roll), math.sin(roll)
    rotate_y = np.array(
        ((cy, 0.0, -sy), (0.0, 1.0, 0.0), (sy, 0.0, cy))
    )
    rotate_x = np.array(
        ((1.0, 0.0, 0.0), (0.0, cx, sx), (0.0, -sx, cx))
    )
    rotate_z = np.array(
        ((cz, sz, 0.0), (-sz, cz, 0.0), (0.0, 0.0, 1.0))
    )
    return rotate_y @ rotate_x @ rotate_z


def _estimate_wheel_position_offsets(frame_bytes):
    """Estimate stable wheel centres in the template body's local space."""
    frames = [parse_car_frame(frame, 0) for frame in frame_bytes]
    if not frames:
        raise ValueError("Wheel position calibration needs at least one frame")
    samples = {
        "wheelStaticPositionM": [[], [], [], []],
        "wheelPositionM": [[], [], [], []],
    }
    for frame in frames:
        body_position = np.asarray(frame["positionM"], dtype=np.float64)
        world_to_body = _ac_rotation_matrix(frame["rotationRad"]).T
        for field in samples:
            for wheel, position in enumerate(frame[field]):
                local = world_to_body @ (
                    np.asarray(position, dtype=np.float64) - body_position
                )
                samples[field][wheel].append(local)
    return {
        field: [
            np.median(np.asarray(wheel_samples), axis=0)
            for wheel_samples in field_samples
        ]
        for field, field_samples in samples.items()
    }


def _estimate_wheel_yaw_calibration(frame_bytes):
    """Infer per-wheel toe and road-wheel/steering-wheel yaw scale.

    Native replay wheel rotations are world-space YXZ Euler angles. The
    static rear-wheel yaw follows body yaw, while front-wheel yaw adds the
    road-wheel steering angle. Calibrating that scale from the same-car
    template avoids hard-coding a steering ratio for every supported car.
    """
    frames = [parse_car_frame(frame, 0) for frame in frame_bytes]
    if not frames:
        raise ValueError("Wheel yaw calibration needs at least one frame")
    body_yaw = np.asarray([frame["rotationRad"][1] for frame in frames])
    steer = np.radians(
        np.asarray([frame["steerAngleDeg"] for frame in frames])
    )
    calibration = []
    for wheel in range(4):
        wheel_yaw = np.asarray([
            frame["wheelStaticRotationRad"][wheel][1]
            for frame in frames
        ])
        relative = _wrap_pi(wheel_yaw - body_yaw)
        if wheel >= 2:
            calibration.append(
                {
                    "toeRad": float(np.median(relative)),
                    "steerPerSteeringRad": 0.0,
                }
            )
            continue

        usable = (
            np.isfinite(relative)
            & np.isfinite(steer)
            & (np.abs(steer) >= math.radians(2.0))
            & (np.abs(relative) < 0.8)
        )
        if np.count_nonzero(usable) >= 4:
            design = np.column_stack((steer[usable], np.ones(np.count_nonzero(usable))))
            factor, toe = np.linalg.lstsq(
                design, relative[usable], rcond=None
            )[0]
            residual = relative[usable] - (factor * steer[usable] + toe)
            median = np.median(residual)
            mad = np.median(np.abs(residual - median))
            inlier = np.abs(residual - median) <= max(0.01, 4.0 * 1.4826 * mad)
            if np.count_nonzero(inlier) >= 4:
                factor, toe = np.linalg.lstsq(
                    design[inlier], relative[usable][inlier], rcond=None
                )[0]
        else:
            factor = DEFAULT_WHEEL_STEER_PER_STEERING_RAD
            toe = float(np.median(relative))
        calibration.append(
            {
                "toeRad": float(toe),
                "steerPerSteeringRad": float(factor),
            }
        )
    return calibration


def synth_poses(layout, car_index, mode, speed_ms, json_path=None):
    """Generate pose overrides for every frame of one car."""
    num_frames = layout["cars"][car_index]["numFrames"]
    first_frame = parse_car_frame(layout["cars"][car_index]["frames"][0][1], 0)
    origin = first_frame["positionM"]
    yaw0 = first_frame["rotationRad"][1]
    if json_path is not None:
        with open(json_path, encoding="utf-8") as handle:
            poses = json.load(handle)
        if len(poses) < 2:
            raise ValueError("poses JSON needs at least 2 frames")
        return _resample_poses(poses, num_frames)
    poses = []
    for index in range(num_frames):
        time_s = index * layout["header"]["recordingIntervalMs"] / 1000.0
        if mode == "straight":
            # AC yaw convention: forward = (-sin(yaw), cos(yaw)).
            dx = -math.sin(yaw0) * speed_ms * time_s
            dz = math.cos(yaw0) * speed_ms * time_s
            pos = [origin[0] + dx, origin[1], origin[2] + dz]
            yaw = yaw0
        elif mode == "circle":
            radius = 80.0
            angle = time_s * speed_ms / radius
            pos = [
                origin[0] + radius * math.cos(angle),
                origin[1],
                origin[2] + radius * math.sin(angle),
            ]
            yaw = _wrap_pi(angle)
        else:
            raise ValueError("unknown mode: " + mode)
        velocity = [speed_ms * -math.sin(yaw), 0.0, speed_ms * math.cos(yaw)]
        poses.append(
            {
                "positionM": pos,
                "rotationRad": [0.0, yaw, 0.0],
                "velocityMS": velocity,
                "rpm": 4000.0 + 500.0 * math.sin(index / 20.0),
                "gear": 2,
                "gas": 200,
                "brake": 0,
                "steerAngleDeg": 0.0,
                "currentLapTimeMs": int(round(time_s * 1000.0)),
            }
        )
    return poses


def _resample_poses(poses, num_frames):
    """Linearly resample a sparse pose list to the target frame count."""
    n = len(poses)
    source_t = np.linspace(0.0, 1.0, n)
    target_t = np.linspace(0.0, 1.0, num_frames)
    keys = [k for k in poses[0] if isinstance(poses[0][k], list)]
    scalars = [k for k in poses[0] if not isinstance(poses[0][k], list)]
    result = []
    for frame_t in target_t:
        out = {}
        for key in keys:
            col = np.array([pose[key] for pose in poses], dtype=float)
            out[key] = [
                float(np.interp(frame_t, source_t, col[:, column]))
                for column in range(col.shape[1])
            ]
        for key in scalars:
            col = np.array([pose[key] for pose in poses], dtype=float)
            value = float(np.interp(frame_t, source_t, col))
            out[key] = int(round(value)) if isinstance(poses[0][key], int) else value
        result.append(out)
    return result


def patch_frame(
    frame_bytes,
    pose,
    wheel_yaw_calibration=None,
    wheel_position_offsets=None,
    wheel_steer_multiplier=1.0,
):
    """Overwrite pose fields inside one 256-byte frame (in place)."""
    if wheel_steer_multiplier <= 0.0:
        raise ValueError("Wheel steering multiplier must be positive")
    data = bytearray(frame_bytes)
    original = parse_car_frame(frame_bytes, 0)
    if "positionM" in pose:
        old_position = original["positionM"]
        new_position = pose["positionM"]
        if wheel_position_offsets is not None:
            body_to_world = _ac_rotation_matrix(
                pose.get("rotationRad", original["rotationRad"])
            )
            body_position = np.asarray(new_position, dtype=np.float64)
            for offset, field in (
                (20, "wheelStaticPositionM"),
                (92, "wheelPositionM"),
            ):
                transformed = []
                for local in wheel_position_offsets[field]:
                    transformed.extend(body_position + body_to_world @ local)
                struct.pack_into("<12f", data, offset, *transformed)
        else:
            old_yaw = original["rotationRad"][1]
            new_yaw = pose.get("rotationRad", original["rotationRad"])[1]
            delta_yaw = new_yaw - old_yaw
            cosine = math.cos(delta_yaw)
            sine = math.sin(delta_yaw)
            # Direct callers retain the current frame's suspension geometry.
            for offset in (20, 92):
                wheel_values = struct.unpack_from("<12f", frame_bytes, offset)
                transformed = []
                for wheel in range(4):
                    x, y, z = wheel_values[wheel * 3:wheel * 3 + 3]
                    relative_x = x - old_position[0]
                    relative_z = z - old_position[2]
                    transformed.extend(
                        (
                            new_position[0]
                            + cosine * relative_x
                            - sine * relative_z,
                            new_position[1] + (y - old_position[1]),
                            new_position[2]
                            + sine * relative_x
                            + cosine * relative_z,
                        )
                    )
                struct.pack_into("<12f", data, offset, *transformed)
        struct.pack_into("<3f", data, 0, *pose["positionM"])
    if "rotationRad" in pose or "steerAngleDeg" in pose:
        old_body_yaw = original["rotationRad"][1]
        new_body_yaw = pose.get("rotationRad", original["rotationRad"])[1]
        steer_rad = math.radians(
            pose.get("steerAngleDeg", original["steerAngleDeg"])
        )
        if wheel_yaw_calibration is None:
            wheel_yaw_calibration = [
                {
                    "toeRad": _wrap_pi(
                        original["wheelStaticRotationRad"][wheel][1]
                        - old_body_yaw
                    ),
                    "steerPerSteeringRad": 0.0,
                }
                for wheel in range(4)
            ]
        for wheel, calibration in enumerate(wheel_yaw_calibration):
            desired_static_yaw = _wrap_pi(
                new_body_yaw
                + calibration["toeRad"]
                + calibration["steerPerSteeringRad"]
                * steer_rad
                * wheel_steer_multiplier
            )
            old_static_yaw = original["wheelStaticRotationRad"][wheel][1]
            yaw_delta = _wrap_pi(desired_static_yaw - old_static_yaw)
            dynamic_yaw = _wrap_pi(
                original["wheelRotationRad"][wheel][1] + yaw_delta
            )
            # Both rotation triplets are stored YXZ, so yaw is the first
            # binary16 value. Adding world yaw preserves the dynamic wheel's
            # rolling orientation instead of replacing it.
            struct.pack_into("<e", data, 68 + wheel * 6, desired_static_yaw)
            struct.pack_into("<e", data, 140 + wheel * 6, dynamic_yaw)
    if "rotationRad" in pose:
        rx, ry, rz = pose["rotationRad"]
        struct.pack_into("<3e", data, 12, ry, rx, rz)  # stored YXZ
    if "velocityMS" in pose:
        struct.pack_into("<3e", data, 164, *pose["velocityMS"])
    if "rpm" in pose:
        struct.pack_into("<e", data, 170, float(pose["rpm"]))
    if "steerAngleDeg" in pose:
        struct.pack_into("<e", data, 212, float(pose["steerAngleDeg"]))
    for key, offset in (
        ("slipAngleRad", 180),
        ("slipRatio", 188),
        ("ndSlip", 196),
    ):
        if key in pose:
            struct.pack_into("<4e", data, offset, *pose[key])
    if "currentLapTimeMs" in pose:
        struct.pack_into("<I", data, 220, int(pose["currentLapTimeMs"]))
    if "gear" in pose:
        data[234] = int(pose["gear"])
    if "gas" in pose:
        data[244] = int(pose["gas"])
    if "brake" in pose:
        data[245] = int(pose["brake"])
    return bytes(data)


def morph(
    template_path,
    output_path,
    car_index=0,
    mode="straight",
    speed_ms=25.0,
    json_path=None,
    poses=None,
    wheel_steer_multiplier=1.0,
):
    if wheel_steer_multiplier <= 0.0:
        raise ValueError("Wheel steering multiplier must be positive")
    raw = Path(template_path).read_bytes()
    layout = locate(raw)
    if car_index < 0 or car_index >= len(layout["cars"]):
        raise ValueError("car index {} out of range".format(car_index))
    if poses is None:
        poses = synth_poses(layout, car_index, mode, speed_ms, json_path)
    elif len(poses) != layout["cars"][car_index]["numFrames"]:
        raise ValueError(
            "Pose count {} does not match template frame count {}".format(
                len(poses), layout["cars"][car_index]["numFrames"]
            )
        )
    wheel_yaw_calibration = _estimate_wheel_yaw_calibration(
        [frame for _, frame in layout["cars"][car_index]["frames"]]
    )
    wheel_position_offsets = _estimate_wheel_position_offsets(
        [frame for _, frame in layout["cars"][car_index]["frames"]]
    )
    out = bytearray()
    out += layout["headerBytes"]
    out += layout["globalBytes"]
    for ci, car_layout in enumerate(layout["cars"]):
        out += car_layout["headerBytes"]
        for index in range(car_layout["numFrames"]):
            frame_header, frame = car_layout["frames"][index]
            if ci == car_index and index < len(poses):
                frame = patch_frame(
                    frame,
                    poses[index],
                    wheel_yaw_calibration,
                    wheel_position_offsets,
                    wheel_steer_multiplier,
                )
            out += frame_header + frame
            out += car_layout["wings"][index]
        out += car_layout["trailingBytes"]
    out += raw[layout["globalEnd"] + car_section_size(layout):layout["footer"]]
    out += raw[layout["footer"]:]
    Path(output_path).write_bytes(out)
    return len(out)


def car_section_size(layout):
    return sum(
        len(c["headerBytes"])
        + c["numFrames"] * (FRAME_HEADER_SIZE + CAR_FRAME_SIZE)
        + sum(len(w) for w in c["wings"])
        + 4
        + c["trailing"] * CSP_TRAILING_ENTRY_BYTES
        for c in layout["cars"]
    )


def pack_car_frame(frame):
    """Serialize a parsed frame dict back to 256 bytes (lossless)."""
    values = [0.0] * 106
    values[0:3] = frame["positionM"]
    rx, ry, rz = frame["rotationRad"]
    values[3:6] = (ry, rx, rz)
    for index, group in enumerate(
        (
            "wheelStaticPositionM",
            "wheelStaticRotationRad",
            "wheelPositionM",
            "wheelRotationRad",
        )
    ):
        stored_yxz = index in (1, 3)  # rotations are stored YXZ
        for wheel in range(4):
            base = 6 + index * 12 + wheel * 3
            x, y, z = frame[group][wheel]
            values[base:base + 3] = (y, x, z) if stored_yxz else (x, y, z)
    values[54:57] = frame["velocityMS"]
    values[57] = frame["rpm"]
    for offset, key in (
        (58, "wheelAngularVelocityRads"),
        (62, "slipAngleRad"),
        (66, "slipRatio"),
        (70, "ndSlip"),
        (74, "loadN"),
    ):
        values[offset:offset + 4] = frame[key]
    values[78] = frame["steerAngleDeg"]
    values[79] = frame["bodyworkNoise"]
    values[80] = frame["drivetrainSpeed"]
    values[81:84] = (
        frame["currentLapTimeMs"],
        frame["lastLapTimeMs"],
        frame["bestLapTimeMs"],
    )
    values[84:87] = (frame["fuel"], frame["fuelPerLap"], frame["gear"])
    values[87:91] = frame["tireDirt"]
    values[91:96] = (
        frame["damageFrontDeformation"],
        frame["damageRear"],
        frame["damageLeft"],
        frame["damageRight"],
        frame["damageFront"],
    )
    values[96:98] = (frame["gas"], frame["brake"])
    values[98:100] = (frame["currentLap"], frame["unknown"])
    values[100] = frame["status"]
    values[101] = frame["unknown2"]
    values[102:105] = (frame["dirt"], frame["engineHealth"], frame["boost"])
    values[105] = frame.get("trailingByte", 0)
    for index in range(81, 106):
        values[index] = int(values[index])
    return CAR_FRAME.pack(*values)


# EXT_PERCAR v6/v7 per-frame byte offsets by field kind (community layout).
EXT_CAR_F32_OFFSETS = (12, 32, 52, 72)
EXT_CAR_F16_OFFSETS = (
    4, 6, 8, 10, 16, 18, 20, 22, 24, 26, 28, 30,
    36, 38, 44, 46, 48, 50, 56, 58, 64, 66, 68, 70,
    76, 78, 103, 105,
)


def _rebuild_ext_car_stream(stream, old, num_frames):
    """Interpolate the float fields of an EXT_PERCAR stream and hold the
    integer/byte fields at their first-frame values."""
    frames = np.frombuffer(stream, dtype=np.uint8).reshape(old, -1)
    bytes_per_frame = frames.shape[1]
    new = np.zeros((num_frames, bytes_per_frame), dtype=np.uint8)
    new[:] = frames[0]  # hold int/byte fields at first-frame values
    source = np.linspace(0.0, 1.0, old)
    target = np.linspace(0.0, 1.0, num_frames)
    for offset in EXT_CAR_F32_OFFSETS:
        if offset + 4 <= bytes_per_frame:
            col = frames[:, offset:offset + 4].copy().view(np.float32)[:, 0]
            new[:, offset:offset + 4] = (
                np.interp(target, source, col)[:, None].astype(np.float32)
            ).view(np.uint8)
    for offset in EXT_CAR_F16_OFFSETS:
        if offset + 2 <= bytes_per_frame:
            col = frames[:, offset:offset + 2].copy().view(np.float16)[:, 0]
            new[:, offset:offset + 2] = (
                np.interp(target, source, col)[:, None].astype(np.float16)
            ).view(np.uint8)
    return new.tobytes()


def _resample_records_nearest(stream, old, num_frames, bytes_per_frame):
    """Resize unknown mixed-layout records without altering their bytes."""
    frames = np.frombuffer(stream, dtype=np.uint8).reshape(old, -1)
    if frames.shape[1] != bytes_per_frame:
        raise ValueError(
            "Expected {} bytes per frame, found {}".format(
                bytes_per_frame, frames.shape[1]
            )
        )
    indices = np.rint(np.linspace(0, old - 1, num_frames)).astype(int)
    return frames[indices].tobytes()


def resample(template_path, output_path, num_frames, stream_content="interp"):
    """Rebuild the replay at a new frame count (interpolated).

    ``stream_content`` selects how the per-frame CSP extension streams
    are rebuilt: ``interp`` (default, field-wise interpolation),
    ``zeros`` (correct length, zero content), or ``firstframe`` (correct
    length, first frame's content repeated).
    """
    if num_frames < 2:
        raise ValueError("Replay resampling needs at least two frames")
    raw = Path(template_path).read_bytes()
    layout = locate(raw)
    header = dict(layout["header"])
    header["numFrames"] = num_frames
    header["currentRecordingIndex"] = num_frames
    old = layout["header"]["numFrames"]
    out = bytearray()
    out += pack_header(header)
    # Global records mix binary16 and unresolved track-object data. Select
    # complete source records so resampling cannot manufacture invalid bits.
    global_bytes_per_frame = len(layout["globalBytes"]) // old
    out += _resample_records_nearest(
        layout["globalBytes"], old, num_frames, global_bytes_per_frame
    )
    # Cars: interpolate parsed frames and keep template wing bytes.
    for car_layout in layout["cars"]:
        car_header_bytes = bytearray(car_layout["headerBytes"])
        old_frames = [parse_car_frame(frame, 0) for _, frame in car_layout["frames"]]
        matrix = _frames_to_matrix(old_frames)
        new_matrix = _interp_columns(matrix, num_frames)
        # A rolling wheel repeatedly crosses YXZ Euler singularities. Linear
        # interpolation of the three stored components independently invents
        # orientations that never existed, making the axle jump by more than
        # 100 degrees between frames. Keep each static/dynamic wheel rotation
        # as one native triplet; morph() later applies body and steering yaw to
        # the complete orientation without disturbing its rolling pose.
        nearest = np.rint(
            np.linspace(0, len(old_frames) - 1, num_frames)
        ).astype(int)
        for start, end in ((18, 30), (42, 54)):
            new_matrix[:, start:end] = matrix[nearest, start:end]
        new_frames = _matrix_to_frames(new_matrix)
        for frame in new_frames:
            # Trailing byte is a constant flag in native recordings; the
            # matrix path cannot represent it, so hold the first value.
            frame["trailingByte"] = old_frames[0].get("trailingByte", 1)
        # patch the per-car numFrames u32 (after the five lstrings)
        offset = 0
        for _ in range(5):
            (n,) = struct.unpack_from("<I", car_header_bytes, offset)
            offset += 4 + n
        struct.pack_into("<I", car_header_bytes, offset, num_frames)
        out += bytes(car_header_bytes)
        wing = car_layout["wings"][0] if car_layout["wings"] else b""
        first_header = car_layout["frames"][0][0]
        (
            _timestamp,
            ambient_temp,
            road_temp,
            wind_speed,
            wind_direction,
        ) = struct.unpack("<I4f", first_header)
        # Native recordings store zero frame-header timestamps; the
        # replay manager is observed to reject non-zero ones.
        frame_header = struct.pack(
            "<I4f",
            0,
            ambient_temp,
            road_temp,
            wind_speed,
            wind_direction,
        )
        for index in range(num_frames):
            out += frame_header
            out += pack_car_frame(new_frames[index])
            out += wing
        out += car_layout["trailingBytes"]
    # CSP: rebuild per-frame streams, copy everything else verbatim.
    # The region between the last car and the CSP section is a session
    # data table: [u32 groupCount][groupCount x numCars x 20 bytes], each
    # entry [u32 typeIndex][u32 x4].  The loader indexes a handler table
    # with typeIndex, so dropping or corrupting this region makes it read
    # garbage indices out of bounds and crash.  Copy it verbatim.
    car_section_end = layout["cars"][-1]["start"]
    car_section_end += len(layout["cars"][-1]["headerBytes"])
    car_section_end += layout["cars"][-1]["numFrames"] * (
        FRAME_HEADER_SIZE + CAR_FRAME_SIZE
    )
    car_section_end += sum(len(w) for w in layout["cars"][-1]["wings"])
    car_section_end += 4 + layout["cars"][-1]["trailing"] * CSP_TRAILING_ENTRY_BYTES
    prelude_end, csp_records = _walk_csp_records(raw, layout["cspOffset"])
    if not csp_records:
        raise ValueError("Template CSP section has no extension records")
    out += raw[car_section_end:layout["cspOffset"]]
    csp_offset = len(out)
    out += raw[layout["cspOffset"]:prelude_end]
    for record in csp_records:
        out += struct.pack("<I", len(record["name"])) + record["name"]
        payload = record["payload"]
        if record["bytesPerFrame"] and record["compressed"]:
            stream = zlib.decompress(payload)
            if stream_content == "zeros":
                payload = zlib.compress(b"\x00" * (record["bytesPerFrame"] * num_frames), 1)
            elif stream_content == "firstframe":
                payload = zlib.compress(
                    stream[:record["bytesPerFrame"]] * num_frames, 1
                )
            elif record["name"].startswith(b"EXT_PERCAR"):
                payload = zlib.compress(
                    _rebuild_ext_car_stream(stream, old, num_frames), 1
                )
            else:
                # EXT_PERFRAME/PERRACEFRAME contain mixed or incompletely
                # documented fields. Preserve each source record exactly
                # and select the nearest one on the new time grid.
                payload = zlib.compress(
                    _resample_records_nearest(
                        stream, old, num_frames, record["bytesPerFrame"]
                    ),
                    1,
                )
        elif record["name"].startswith(b"EXT_EXTRASTREAM"):
            # The extra-stream payload is [u32 streamId][u32 metadata]
            # followed by zlib; it holds one sample slot per 16 frames and
            # CSP indexes it by frame, so its length must match the new
            # frame count or the player reads out of bounds.
            stream = zlib.decompress(payload[8:])
            slots = (num_frames + 15) // 16
            if len(stream) != slots:
                new_stream = stream[:slots] + b"\x00" * max(0, slots - len(stream))
                payload = payload[:8] + zlib.compress(new_stream, 1)
        out += struct.pack("<I", len(payload)) + payload
    out += raw[csp_records[-1]["end"]:layout["footer"]]
    out += POSTFIX
    out += struct.pack("<II", csp_offset, 1)
    Path(output_path).write_bytes(out)
    return len(out)


def _interp_columns(matrix, num_frames):
    """Linear interpolation along axis 0 from a float64 (n, m) matrix."""
    matrix = np.asarray(matrix, dtype=np.float64)
    n = matrix.shape[0]
    source = np.linspace(0.0, 1.0, n)
    target = np.linspace(0.0, 1.0, num_frames)
    result = np.empty((num_frames, matrix.shape[1]), dtype=np.float64)
    for column in range(matrix.shape[1]):
        result[:, column] = np.interp(target, source, matrix[:, column])
    return result


def _frames_to_matrix(frames):
    rows = []
    for frame in frames:
        row = frame["positionM"] + frame["rotationRad"]
        for group in (
            "wheelStaticPositionM",
            "wheelStaticRotationRad",
            "wheelPositionM",
            "wheelRotationRad",
        ):
            for wheel in frame[group]:
                row += wheel
        row += frame["velocityMS"]
        row += [frame["rpm"]]
        row += frame["wheelAngularVelocityRads"]
        row += frame["slipAngleRad"]
        row += frame["slipRatio"]
        row += frame["ndSlip"]
        row += frame["loadN"]
        row += [frame["steerAngleDeg"], frame["bodyworkNoise"], frame["drivetrainSpeed"]]
        row += [
            frame["currentLapTimeMs"],
            frame["lastLapTimeMs"],
            frame["bestLapTimeMs"],
        ]
        row += [frame["fuel"], frame["fuelPerLap"], frame["gear"]]
        row += frame["tireDirt"]
        row += [
            frame["damageFrontDeformation"],
            frame["damageRear"],
            frame["damageLeft"],
            frame["damageRight"],
            frame["damageFront"],
        ]
        row += [frame["gas"], frame["brake"]]
        row += [frame["currentLap"], frame["unknown"]]
        row += [frame["status"], frame["unknown2"]]
        row += [frame["dirt"], frame["engineHealth"], frame["boost"]]
        rows.append(row)
    return np.asarray(rows)


def _matrix_to_frames(matrix):
    frames = []
    for row in matrix:
        row = row.tolist()
        frames.append(
            {
                "positionM": row[0:3],
                "rotationRad": row[3:6],
                "wheelStaticPositionM": _split_xyz(row[6:18]),
                "wheelStaticRotationRad": _split_xyz(row[18:30]),
                "wheelPositionM": _split_xyz(row[30:42]),
                "wheelRotationRad": _split_xyz(row[42:54]),
                "velocityMS": row[54:57],
                "rpm": row[57],
                "wheelAngularVelocityRads": row[58:62],
                "slipAngleRad": row[62:66],
                "slipRatio": row[66:70],
                "ndSlip": row[70:74],
                "loadN": row[74:78],
                "steerAngleDeg": row[78],
                "bodyworkNoise": row[79],
                "drivetrainSpeed": row[80],
                "currentLapTimeMs": int(row[81]),
                "lastLapTimeMs": int(row[82]),
                "bestLapTimeMs": int(row[83]),
                "fuel": int(row[84]),
                "fuelPerLap": int(row[85]),
                "gear": int(row[86]),
                "tireDirt": [int(v) for v in row[87:91]],
                "damageFrontDeformation": int(row[91]),
                "damageRear": int(row[92]),
                "damageLeft": int(row[93]),
                "damageRight": int(row[94]),
                "damageFront": int(row[95]),
                "gas": int(row[96]),
                "brake": int(row[97]),
                "currentLap": int(row[98]),
                "unknown": int(row[99]),
                "status": int(row[100]),
                "unknown2": int(row[101]),
                "dirt": int(row[102]),
                "engineHealth": int(row[103]),
                "boost": int(row[104]),
            }
        )
    return frames


def _split_xyz(values):
    return [
        values[index * 3:index * 3 + 3]
        for index in range(4)
    ]


def _walk_csp_records(raw, offset):
    """Parse the CSP extension records with their byte ranges.

    Returns ``(prelude_end, records)`` where the prelude is the
    length-prefixed strings and the INI blob before the first record.
    """
    while offset + 4 <= len(raw):
        (length,) = struct.unpack_from("<I", raw, offset)
        if length > 255:
            offset += 4 + length
            break
        offset += 4 + length
    prelude_end = offset
    records = []
    while offset + 8 <= len(raw):
        (name_length,) = struct.unpack_from("<I", raw, offset)
        if name_length <= 0 or name_length > 255:
            break
        name = raw[offset + 4:offset + 4 + name_length]
        if not (name.startswith(b"EXT_") or name.startswith(b"__AC_SHADERS")):
            break
        start = offset
        offset += 4 + name_length
        (payload_length,) = struct.unpack_from("<I", raw, offset)
        payload = raw[offset + 4:offset + 4 + payload_length]
        end = offset + 4 + payload_length
        bytes_per_frame = None
        compressed = False
        try:
            decompressed = zlib.decompress(payload)
            compressed = True
        except zlib.error:
            decompressed = None
        if decompressed is not None:
            bytes_per_frame = None
            if name.startswith(b"EXT_PERFRAME"):
                bytes_per_frame = 56
            elif name.startswith(b"EXT_PERRACEFRAME"):
                bytes_per_frame = 16
            elif name.startswith(b"EXT_PERCAR"):
                try:
                    version = int(name.split(b"_v")[1].split(b":")[0])
                except (IndexError, ValueError):
                    version = 0
                bytes_per_frame = {6: 108, 7: 108}.get(version)
        records.append(
            {
                "start": start,
                "end": end,
                "name": bytes(name),
                "payload": payload,
                "bytesPerFrame": bytes_per_frame,
                "compressed": compressed,
            }
        )
        offset = end
    return prelude_end, records
