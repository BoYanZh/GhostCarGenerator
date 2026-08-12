"""Tests for MoTeC LD to Assetto Corsa replay conversion helpers."""

import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost_car.ld_replay import (
    align_replay_heights,
    build_poses_from_xyz,
    smooth_replay_positions,
    validate_track_reference,
)
from ghost_car.motec import CHANNEL_ALIASES


class ReplayHeightAlignmentTest(unittest.TestCase):
    def setUp(self):
        self.reference = np.array(
            [
                [0.0, 10.0, 0.0],
                [10.0, 20.0, 0.0],
                [10.0, 30.0, 10.0],
                [0.0, 40.0, 10.0],
            ]
        )
        self.mapped = np.array(
            [
                [5.0, 100.0, 0.0],
                [10.0, 101.0, 5.0],
                [5.0, 102.0, 10.0],
            ]
        )

    def test_track_mode_uses_reference_height_profile(self):
        aligned, diagnostics = align_replay_heights(
            self.mapped, self.reference, mode="track", offset_m=0.25
        )
        np.testing.assert_allclose(aligned[:, 1], [15.25, 25.25, 35.25])
        np.testing.assert_allclose(aligned[:, [0, 2]], self.mapped[:, [0, 2]])
        self.assertEqual(diagnostics["heightMode"], "track")
        self.assertAlmostEqual(diagnostics["afterVerticalRmseM"], 0.25)

    def test_gps_offset_mode_only_removes_vertical_datum(self):
        reference_heights = np.array([15.0, 25.0, 35.0])
        mapped = self.mapped.copy()
        mapped[:, 1] = reference_heights + 50.0
        aligned, diagnostics = align_replay_heights(
            mapped, self.reference, mode="gps-offset"
        )
        np.testing.assert_allclose(aligned[:, 1], reference_heights)
        self.assertAlmostEqual(diagnostics["verticalDatumOffsetM"], 50.0)

    def test_gps_mode_preserves_input_height(self):
        aligned, _ = align_replay_heights(
            self.mapped, self.reference, mode="gps", offset_m=-0.5
        )
        np.testing.assert_allclose(aligned[:, 1], self.mapped[:, 1] - 0.5)


class ReplayPoseTest(unittest.TestCase):
    def test_default_gas_source_does_not_use_throttle_plate(self):
        aliases = {name.casefold() for name in CHANNEL_ALIASES["throttle"]}
        self.assertIn("accelerator pos", aliases)
        self.assertNotIn("throttle pos", aliases)
        self.assertNotIn("throttle", aliases)

    def test_pose_builder_zeroes_accelerator_sensor_release_offset(self):
        points = []
        accelerator = [10.6] * 12 + [30.0, 100.0, 100.0]
        for index, value in enumerate(accelerator):
            points.append(
                {
                    "timeS": index * 0.05,
                    "speedMS": 10.0,
                    "headingRad": 0.0,
                    "pitchRad": 0.0,
                    "rollRad": 0.0,
                    "rpm": 3000.0,
                    "throttle": value,
                    "brake": 0.0,
                    "steerRad": 0.0,
                    "gear": 2,
                }
            )
        xyz = np.column_stack(
            (
                np.zeros(len(points)),
                np.zeros(len(points)),
                np.arange(len(points), dtype=float),
            )
        )

        poses, _ = build_poses_from_xyz(points, xyz, np.eye(2))
        gas = np.asarray([pose["gas"] for pose in poses])

        self.assertTrue(np.all(gas[:35] == 0))
        self.assertEqual(int(np.max(gas)), 255)

    def test_pose_builder_derives_pitch_from_aligned_track_grade(self):
        points = []
        for index in range(10):
            points.append(
                {
                    "timeS": index * 0.05,
                    "speedMS": 10.0,
                    "headingRad": 0.0,
                    "pitchRad": 0.0,
                    "rollRad": 0.0,
                    "rpm": 3000.0,
                    "throttle": 1.0,
                    "brake": 0.0,
                    "steerRad": 0.0,
                    "gear": 2,
                }
            )
        z = np.arange(10, dtype=float)
        xyz = np.column_stack((np.zeros(10), 0.1 * z, z))

        poses, _ = build_poses_from_xyz(points, xyz, np.eye(2))

        pitch = np.asarray([pose["rotationRad"][0] for pose in poses])
        self.assertAlmostEqual(
            float(np.median(pitch)), math.atan(0.1), delta=0.005
        )

    def test_pose_builder_wraps_yaw_and_maps_controls(self):
        points = [
            {
                "timeS": 0.0,
                "speedMS": 10.0,
                "headingRad": 0.0,
                "pitchRad": 0.0,
                "rollRad": 0.0,
                "rpm": 3000.0,
                "throttle": 0.5,
                "brake": 0.0,
                "steerRad": 0.1,
                "gear": 1,
            },
            {
                "timeS": 0.03,
                "speedMS": 11.0,
                "headingRad": 2.0 * math.pi - 0.01,
                "pitchRad": 0.0,
                "rollRad": 0.0,
                "rpm": 3200.0,
                "throttle": 1.0,
                "brake": 0.0,
                "steerRad": 0.2,
                "gear": 2,
            },
        ]
        xyz = np.array([[0.0, 5.0, 0.0], [0.0, 5.0, 0.3]])
        poses, frame_count = build_poses_from_xyz(points, xyz, np.eye(2))
        self.assertEqual(frame_count, 3)
        self.assertTrue(all(-math.pi <= p["rotationRad"][1] <= math.pi for p in poses))
        self.assertEqual(poses[0]["gear"], 2)
        self.assertEqual(poses[-1]["gear"], 3)
        self.assertEqual(poses[-1]["gas"], 255)

    def test_pose_builder_rejects_isolated_heading_outlier(self):
        points = []
        headings = [0.0, 0.01, 2.4, 0.03, 0.04]
        for index, heading in enumerate(headings):
            points.append(
                {
                    "timeS": index * 0.05,
                    "speedMS": 10.0,
                    "headingRad": heading,
                    "pitchRad": 0.0,
                    "rollRad": 0.0,
                    "rpm": 3000.0,
                    "throttle": 1.0,
                    "brake": 0.0,
                    "steerRad": 0.0,
                    "gear": 2,
                }
            )
        xyz = np.column_stack(
            (np.zeros(5), np.zeros(5), np.arange(5, dtype=float))
        )
        poses, _ = build_poses_from_xyz(points, xyz, np.eye(2))
        yaw = np.unwrap([pose["rotationRad"][1] for pose in poses])
        self.assertLess(np.max(np.abs(np.diff(yaw))), 0.1)


class ReplayPositionSmoothingTest(unittest.TestCase):
    def test_smoothing_reduces_lateral_jitter_without_moving_height(self):
        positions = np.column_stack(
            (
                np.array([0.2, -0.2] * 10),
                np.linspace(5.0, 7.0, 20),
                np.arange(20, dtype=float),
            )
        )
        smoothed, diagnostics = smooth_replay_positions(
            positions, frequency_hz=20.0, window_s=0.45
        )
        self.assertLess(np.std(smoothed[:, 0]), np.std(positions[:, 0]) * 0.5)
        np.testing.assert_allclose(smoothed[:, 1], positions[:, 1])
        self.assertEqual(diagnostics["positionSmoothingSamples"], 9)

    def test_smoothing_does_not_join_lap_end_to_start(self):
        positions = np.column_stack(
            (
                np.zeros(21),
                np.zeros(21),
                np.arange(21, dtype=float),
            )
        )
        positions[-1, 0] = 1000.0
        smoothed, _ = smooth_replay_positions(
            positions, frequency_hz=20.0, window_s=0.45
        )
        self.assertAlmostEqual(smoothed[0, 0], 0.0, places=9)
        self.assertGreater(smoothed[-1, 0], 500.0)


class ReplayTrackValidationTest(unittest.TestCase):
    def test_rejects_wrong_template_layout(self):
        replay = {
            "header": {
                "track": "example_circuit",
                "trackConfig": "layout_a",
            }
        }
        track_ref = {
            "track": {
                "name": "example_circuit",
                "layout": "layout_b",
            }
        }
        with self.assertRaisesRegex(ValueError, "layout"):
            validate_track_reference(replay, track_ref)


if __name__ == "__main__":
    unittest.main()
