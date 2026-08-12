"""Tests for the Assetto Corsa .acreplay parser."""

import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost_car.acreplay import (
    CAR_FRAME,
    CAR_FRAME_SIZE,
    EXT_CAR_BYTES_PER_FRAME,
    POSTFIX,
    VERSION,
    decompress_payload,
    find_csp_data_offset,
    parse_acreplay,
    parse_car_frame,
    parse_header,
)
from ghost_car.replay_writer import (
    _ac_rotation_matrix,
    _estimate_wheel_yaw_calibration,
    locate,
    morph,
    patch_frame,
    resample,
)

FRAME_HEADER = struct.Struct("<I4f")


def lstring(text):
    raw = text.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def build_frame(offset, x, y, z, rpm=0.0, gear=0, gas=0, brake=0, lap_ms=0):
    """Pack a 256-byte physics frame; most fields stay zero."""
    values = [0.0] * 106
    values[0:3] = (x, y, z)
    values[3:6] = (0.0, 0.5, 0.0)
    values[54:57] = (1.0, 0.0, 0.0)
    values[57] = rpm
    values[78] = 0.0
    values[81:84] = (lap_ms, 0, 0)
    values[84] = 128
    values[86] = gear
    values[96:98] = (gas, brake)
    values[105] = 1
    for index in range(81, len(values)):
        values[index] = int(values[index])
    return CAR_FRAME.pack(*values)


def build_replay(
    num_frames=3,
    num_cars=1,
    with_csp=True,
    version=VERSION,
    cars=None,
    wings=2,
    track_objects=0,
    trailing_entries=b"",
):
    """Construct a minimal synthetic replay as bytes."""
    out = bytearray()
    out += struct.pack("<Id", version, 15.0)
    out += lstring("sol_03_scattered_clouds")
    out += lstring("test_track")
    out += lstring("")
    cars = cars or ["Test Driver"]
    out += struct.pack("<IIII", len(cars), num_frames, num_frames, track_objects)
    global_bytes_per_frame = 4 + 12 * track_objects
    for index in range(num_frames):
        value = 0xFF if index == 0 else index
        out += bytes([value]) * global_bytes_per_frame
    wing_bytes = wings * 4
    for car_index in range(len(cars)):
        out += lstring("ig_test_car")
        out += lstring(cars[car_index])
        out += lstring("US")
        out += lstring("Team X")
        out += lstring("skin_01")
        out += struct.pack("<II", num_frames, wings)
        for index in range(num_frames):
            out += FRAME_HEADER.pack(index * 15, 20.0, 25.0, 1.5, 90.0)
            out += build_frame(
                car_index,
                float(index * 100),
                10.0 + car_index,
                5.0,
                rpm=3000.0 + index,
                gear=2,
                gas=200,
                brake=30,
                lap_ms=index * 1000,
            )
            if index < num_frames - 1:
                out += b"\x00" * wing_bytes
        out += b"\x00" * wing_bytes
        if len(trailing_entries) % 8:
            raise ValueError("trailing entries must be a multiple of 8 bytes")
        out += struct.pack("<I", len(trailing_entries) // 8)
        out += trailing_entries
    if with_csp:
        ini_text = (
            "[CAR_0]\nDRIVER_NAME='Test Driver'\nSKIN=skin_01\n"
            + "PADDING=" + "x" * 300
        )
        ini_start = len(out)
        out += struct.pack("<I", len(ini_text.encode())) + ini_text.encode()
        per_frame = struct.pack("<I", 0x6A45AA00) + b"\x00" * 4
        out += lstring("EXT_PERRACE_v1") + struct.pack("<I", 8) + per_frame
        stream = b"".join(
            b"\xFF\xFF\xFF\xFF" + build_frame(0, i, 0, 0)[:52]
            for i in range(num_frames)
        )
        payload = zlib.compress(stream)
        out += lstring("EXT_PERFRAME_v1") + struct.pack("<I", len(payload)) + payload
        extra_stream = b"".join(
            struct.pack("<I", 1) + struct.pack("<I", i) + b"\x00" * 100
            for i in range(num_frames)
        )[:108 * num_frames]
        payload = zlib.compress(extra_stream)
        out += lstring("EXT_PERCAR_v7:0") + struct.pack("<I", len(payload)) + payload
        chunk = b"\xAA" * 4 + b"\xBB" * 4 + zlib.compress(b"\x00" * 40)
        out += lstring("EXT_EXTRASTREAM_v1") + struct.pack("<I", len(chunk)) + chunk
        out += POSTFIX
        out += struct.pack("<II", ini_start, 1)
    return bytes(out)


class AcreplayHeaderTest(unittest.TestCase):
    def test_header_fields(self):
        raw = build_replay()
        header, offset = parse_header(raw)
        self.assertEqual(header["version"], 16)
        self.assertEqual(header["recordingIntervalMs"], 15.0)
        self.assertEqual(header["weather"], "sol_03_scattered_clouds")
        self.assertEqual(header["track"], "test_track")
        self.assertEqual(header["trackConfig"], "")
        self.assertEqual(header["numCars"], 1)
        self.assertEqual(header["numFrames"], 3)
        self.assertEqual(header["numTrackObjects"], 0)

    def test_unsupported_version(self):
        raw = build_replay(version=17)
        with self.assertRaises(ValueError):
            parse_acreplay(raw)


class AcreplayFrameTest(unittest.TestCase):
    def test_car_frame_round_trip(self):
        raw_frame = build_frame(2, 12.5, -3.25, 44.0, rpm=6500.0, gear=3, gas=255)
        frame = parse_car_frame(raw_frame, 0)
        self.assertEqual(frame["positionM"], [12.5, -3.25, 44.0])
        self.assertEqual(frame["rpm"], 6500.0)
        self.assertEqual(frame["gear"], 3)
        self.assertEqual(frame["gas"], 255)
        self.assertEqual(frame["brake"], 0)
        self.assertEqual(frame["currentLapTimeMs"], 0)
        self.assertAlmostEqual(frame["rotationRad"][0], 0.5)
        self.assertEqual(frame["velocityMS"], [1.0, 0.0, 0.0])

    def test_frame_size_constant(self):
        self.assertEqual(CAR_FRAME.size, 256)
        self.assertEqual(CAR_FRAME_SIZE, 256)


class AcreplayParseTest(unittest.TestCase):
    def test_parse_full(self):
        raw = build_replay(num_cars=2, cars=["Alice", "Bob"])
        result = parse_acreplay(raw)
        header = result["header"]
        self.assertEqual(header["numCars"], 2)
        self.assertEqual(len(result["cars"]), 2)
        car = result["cars"][0]
        self.assertEqual(car["carID"], "ig_test_car")
        self.assertEqual(car["driverName"], "Alice")
        self.assertEqual(car["nationCode"], "US")
        self.assertEqual(car["carSkinID"], "skin_01")
        self.assertEqual(car["numFrames"], 3)
        self.assertEqual(car["numWings"], 2)
        self.assertEqual(car["frameCountDecoded"], 3)
        self.assertEqual(car["trailingCount"], 0)
        frame = car["frames"][1]
        self.assertEqual(frame["timestampMs"], 15)
        self.assertEqual(frame["ambientTempC"], 20.0)
        self.assertEqual(frame["windSpeedMS"], 1.5)
        self.assertEqual(frame["positionM"][0], 100.0)
        self.assertEqual(frame["currentLapTimeMs"], 1000)
        second = result["cars"][1]["frames"][0]
        self.assertEqual(second["positionM"], [0.0, 11.0, 5.0])

    def test_max_frames(self):
        raw = build_replay()
        result = parse_acreplay(raw, max_frames=1)
        self.assertEqual(result["cars"][0]["frameCountDecoded"], 1)
        self.assertEqual(len(result["cars"][0]["frames"]), 1)

    def test_include_raw_frames(self):
        raw = build_replay()
        result = parse_acreplay(raw, include_raw_frames=True)
        raw_hex = result["cars"][0]["frames"][0]["rawHex"]
        self.assertEqual(len(raw_hex), 512)
        expected = build_frame(0, 0.0, 10.0, 5.0, rpm=3000.0, gear=2, gas=200, brake=30)
        self.assertEqual(bytes.fromhex(raw_hex), expected)

    def test_csp_records(self):
        raw = build_replay()
        result = parse_acreplay(raw)
        csp = result["csp"]
        self.assertIsNotNone(csp)
        self.assertIn("DRIVER_NAME='Test Driver'", csp["iniText"])
        self.assertEqual(result["cspDataOffset"], raw.find(b"[CAR_0]") - 4)
        records = {r["name"]: r for r in csp["records"]}
        self.assertEqual(records["EXT_PERRACE_v1"]["payloadLength"], 8)
        self.assertEqual(
            records["EXT_PERRACE_v1"]["payload"], struct.pack("<I", 0x6A45AA00) + b"\x00" * 4
        )
        per_frame = records["EXT_PERFRAME_v1"]
        self.assertTrue(per_frame["compressed"])
        self.assertEqual(per_frame["decompressedLength"], 56 * 3)
        self.assertEqual(per_frame["bytesPerFrame"], 56)
        per_car = records["EXT_PERCAR_v7:0"]
        self.assertEqual(per_car["bytesPerFrame"], EXT_CAR_BYTES_PER_FRAME[6])
        stream = records["EXT_EXTRASTREAM_v1"]
        self.assertEqual(stream["streamId"], 0xAAAAAAAA)
        self.assertEqual(stream["streamMetadata"], 0xBBBBBBBB)
        self.assertEqual(decompress_payload(stream["payload"]), b"\x00" * 40)

    def test_no_csp(self):
        raw = build_replay(with_csp=False)
        self.assertIsNone(find_csp_data_offset(raw))
        result = parse_acreplay(raw)
        self.assertIsNone(result["csp"])
        self.assertIsNone(result["cspDataOffset"])

    def test_parse_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.acreplay"
            path.write_bytes(build_replay())
            result = parse_acreplay(str(path))
            self.assertEqual(result["header"]["numCars"], 1)
            self.assertEqual(result["fileSizeBytes"], path.stat().st_size)


class AcreplayFooterTest(unittest.TestCase):
    def test_footer_layout(self):
        raw = build_replay()
        index = raw.rfind(POSTFIX)
        offset = struct.unpack_from("<I", raw, index + len(POSTFIX))[0]
        version = struct.unpack_from("<I", raw, index + len(POSTFIX) + 4)[0]
        self.assertEqual(version, 1)
        self.assertEqual(offset, raw.find(b"[CAR_0]") - 4)


class AcreplayWriterTest(unittest.TestCase):
    def test_patch_frame_keeps_fixed_wheel_centres_in_body_space(self):
        frame = build_frame(0, 10.0, 2.0, 30.0)
        offsets = np.asarray(
            [
                [-0.75, -0.35, 1.25],
                [0.75, -0.35, 1.25],
                [-0.75, -0.35, -1.25],
                [0.75, -0.35, -1.25],
            ]
        )
        wheel_offsets = {
            "wheelStaticPositionM": offsets,
            "wheelPositionM": offsets,
        }
        pose = {
            "positionM": [100.0, 20.0, 30.0],
            "rotationRad": [0.2, 0.3, -0.1],
        }

        patched = parse_car_frame(
            patch_frame(
                frame,
                pose,
                wheel_position_offsets=wheel_offsets,
            ),
            0,
        )

        world_to_body = _ac_rotation_matrix(patched["rotationRad"]).T
        body_position = np.asarray(patched["positionM"])
        for field in wheel_offsets:
            recovered = np.asarray([
                world_to_body @ (np.asarray(wheel) - body_position)
                for wheel in patched[field]
            ])
            np.testing.assert_allclose(recovered, offsets, atol=0.001)

    def test_patch_frame_aligns_wheel_yaw_and_preserves_rolling_pose(self):
        raw = bytearray(build_frame(0, 0.0, 0.0, 0.0))
        old_body_yaw = 0.4
        struct.pack_into("<3e", raw, 12, old_body_yaw, 0.0, 0.0)
        old_static_yaws = [0.5, 0.5, 0.4, 0.4]
        old_dynamic_yaws = [1.1, -0.7, 1.4, -1.0]
        for wheel in range(4):
            struct.pack_into(
                "<3e", raw, 68 + wheel * 6,
                old_static_yaws[wheel], 0.01, 0.02,
            )
            struct.pack_into(
                "<3e", raw, 140 + wheel * 6,
                old_dynamic_yaws[wheel], 0.7, -0.2,
            )
        calibration = [
            {"toeRad": 0.01, "steerPerSteeringRad": 0.08},
            {"toeRad": -0.01, "steerPerSteeringRad": 0.08},
            {"toeRad": 0.002, "steerPerSteeringRad": 0.0},
            {"toeRad": -0.002, "steerPerSteeringRad": 0.0},
        ]
        pose = {
            "rotationRad": [0.0, -0.3, 0.0],
            "steerAngleDeg": 30.0,
            "slipAngleRad": [0.0] * 4,
            "slipRatio": [0.0] * 4,
            "ndSlip": [0.0] * 4,
        }

        patched = parse_car_frame(
            patch_frame(bytes(raw), pose, calibration), 0
        )

        steer_rad = np.radians(pose["steerAngleDeg"])
        expected_yaws = [
            pose["rotationRad"][1]
            + wheel["toeRad"]
            + wheel["steerPerSteeringRad"] * steer_rad
            for wheel in calibration
        ]
        for wheel in range(4):
            self.assertAlmostEqual(
                patched["wheelStaticRotationRad"][wheel][1],
                expected_yaws[wheel],
                delta=0.002,
            )
            old_roll_offset = (
                old_dynamic_yaws[wheel] - old_static_yaws[wheel]
            )
            new_roll_offset = (
                patched["wheelRotationRad"][wheel][1]
                - patched["wheelStaticRotationRad"][wheel][1]
            )
            self.assertAlmostEqual(
                (new_roll_offset + np.pi) % (2 * np.pi) - np.pi,
                (old_roll_offset + np.pi) % (2 * np.pi) - np.pi,
                delta=0.003,
            )
        self.assertEqual(patched["slipAngleRad"], [0.0] * 4)
        self.assertEqual(patched["slipRatio"], [0.0] * 4)
        self.assertEqual(patched["ndSlip"], [0.0] * 4)

    def test_patch_frame_scales_front_wheel_yaw_without_changing_steer(self):
        raw = bytearray(build_frame(0, 0.0, 0.0, 0.0))
        calibration = [
            {"toeRad": 0.0, "steerPerSteeringRad": 0.08},
            {"toeRad": 0.0, "steerPerSteeringRad": 0.08},
            {"toeRad": 0.0, "steerPerSteeringRad": 0.0},
            {"toeRad": 0.0, "steerPerSteeringRad": 0.0},
        ]

        patched = parse_car_frame(
            patch_frame(
                bytes(raw),
                {"steerAngleDeg": 30.0},
                calibration,
                wheel_steer_multiplier=2.0,
            ),
            0,
        )

        expected_front_yaw = 0.08 * np.radians(30.0) * 2.0
        self.assertAlmostEqual(patched["steerAngleDeg"], 30.0, delta=0.01)
        for wheel in (0, 1):
            self.assertAlmostEqual(
                patched["wheelStaticRotationRad"][wheel][1],
                expected_front_yaw,
                delta=0.001,
            )
        for wheel in (2, 3):
            self.assertAlmostEqual(
                patched["wheelStaticRotationRad"][wheel][1],
                0.0,
                delta=0.001,
            )

    def test_wheel_yaw_calibration_recovers_template_steering_scale(self):
        frames = []
        for steer_deg in (-90.0, -45.0, 0.0, 45.0, 90.0):
            raw = bytearray(build_frame(0, 0.0, 0.0, 0.0))
            body_yaw = 0.4
            steer_rad = np.radians(steer_deg)
            struct.pack_into("<3e", raw, 12, body_yaw, 0.0, 0.0)
            struct.pack_into("<e", raw, 212, steer_deg)
            for wheel, toe in enumerate((0.01, -0.01, 0.002, -0.002)):
                factor = 0.08 if wheel < 2 else 0.0
                yaw = body_yaw + toe + factor * steer_rad
                struct.pack_into("<3e", raw, 68 + wheel * 6, yaw, 0.0, 0.0)
            frames.append(bytes(raw))

        calibration = _estimate_wheel_yaw_calibration(frames)

        self.assertAlmostEqual(
            calibration[0]["steerPerSteeringRad"], 0.08, delta=0.002
        )
        self.assertAlmostEqual(
            calibration[1]["steerPerSteeringRad"], 0.08, delta=0.002
        )
        self.assertAlmostEqual(calibration[2]["toeRad"], 0.002, delta=0.002)
        self.assertAlmostEqual(calibration[3]["toeRad"], -0.002, delta=0.002)

    def test_morph_patches_poses_and_preserves_trailing_entries(self):
        raw = build_replay(track_objects=1, trailing_entries=b"trailing")
        poses = [
            {
                "positionM": [100.0 + index, 20.0, 30.0],
                "rotationRad": [0.0, 0.1 * index, 0.0],
                "velocityMS": [1.0, 0.0, 0.0],
                "rpm": 4000.0,
                "gear": 3,
                "gas": 255,
                "brake": 0,
                "steerAngleDeg": 2.0,
                "currentLapTimeMs": index * 15,
            }
            for index in range(3)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template.acreplay"
            output = Path(tmp) / "morphed.acreplay"
            template.write_bytes(raw)
            morph(template, output, poses=poses)
            decoded = parse_acreplay(output, max_frames=0)
            original = parse_acreplay(raw, max_frames=0)
            self.assertEqual(decoded["cars"][0]["frames"][2]["positionM"], [102.0, 20.0, 30.0])
            self.assertEqual(decoded["cars"][0]["frames"][0]["gas"], 255)
            del original
            frames = decoded["cars"][0]["frames"]
            for field in ("wheelStaticPositionM", "wheelPositionM"):
                distances = np.array([
                    [
                        np.linalg.norm(
                            np.asarray(wheel) - np.asarray(frame["positionM"])
                        )
                        for wheel in frame[field]
                    ]
                    for frame in frames
                ])
                # Template suspension travel must not be time-compressed into
                # the generated lap. Wheel centres are fixed in body space.
                np.testing.assert_allclose(
                    distances,
                    np.repeat(distances[0:1], len(distances), axis=0),
                    rtol=1e-5,
                    atol=1e-5,
                )
            self.assertEqual(
                locate(output.read_bytes())["cars"][0]["trailingBytes"],
                locate(raw)["cars"][0]["trailingBytes"],
            )

    def test_resample_rebuilds_frame_dependent_sections(self):
        raw = build_replay(track_objects=1, trailing_entries=b"trailing")
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template.acreplay"
            output = Path(tmp) / "resampled.acreplay"
            template.write_bytes(raw)
            resample(template, output, 5)
            decoded = parse_acreplay(output, max_frames=0)
            self.assertEqual(decoded["header"]["numFrames"], 5)
            self.assertEqual(decoded["cars"][0]["numFrames"], 5)
            old_global = locate(raw)["globalBytes"]
            new_global = locate(output.read_bytes())["globalBytes"]
            old_records = {
                old_global[index * 16:(index + 1) * 16]
                for index in range(3)
            }
            self.assertTrue(all(
                new_global[index * 16:(index + 1) * 16] in old_records
                for index in range(5)
            ))
            records = {record["name"]: record for record in decoded["csp"]["records"]}
            self.assertEqual(records["EXT_PERFRAME_v1"]["decompressedLength"], 56 * 5)
            per_frame = decompress_payload(records["EXT_PERFRAME_v1"]["payload"])
            self.assertEqual(
                [per_frame[index * 56:(index * 56) + 4] for index in range(5)],
                [b"\xFF\xFF\xFF\xFF"] * 5,
            )
            self.assertEqual(
                locate(output.read_bytes())["cars"][0]["trailingBytes"],
                locate(raw)["cars"][0]["trailingBytes"],
            )

    def test_resample_keeps_native_wheel_rotation_triplets(self):
        raw = bytearray(build_replay(num_frames=2))
        car = locate(raw)["cars"][0]
        offset = car["start"] + len(car["headerBytes"])
        source_rotations = (
            (-np.radians(80.0), 0.0, 0.0),
            (-np.radians(80.0), -np.pi, np.pi),
        )
        for index, rotation in enumerate(source_rotations):
            frame_offset = offset + FRAME_HEADER.size
            for wheel in range(4):
                # Stored order is YXZ.
                x, y, z = rotation
                struct.pack_into(
                    "<3e", raw, frame_offset + 140 + wheel * 6, y, x, z
                )
            offset += FRAME_HEADER.size + CAR_FRAME_SIZE
            if index == 0:
                offset += car["numWings"] * 4

        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template.acreplay"
            output = Path(tmp) / "resampled.acreplay"
            template.write_bytes(raw)
            resample(template, output, 3)
            original = parse_acreplay(raw, max_frames=0)["cars"][0]["frames"]
            middle = parse_acreplay(output, max_frames=0)["cars"][0]["frames"][1]
            native = [
                tuple(frame["wheelRotationRad"][0])
                for frame in original
            ]
            self.assertIn(tuple(middle["wheelRotationRad"][0]), native)


if __name__ == "__main__":
    unittest.main()
