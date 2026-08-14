"""Tests for MoTeC LD to Assetto Corsa replay conversion helpers."""

import math
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ghost_car.ld_replay import (
    _pad_comparison_poses,
    _synchronize_lap_progress,
    offset_track_calibration,
    align_replay_heights,
    build_poses_from_xyz,
    convert_ld_to_acreplay,
    gps_track_to_ac,
    smooth_replay_positions,
    validate_track_reference,
)
from ghost_car.motec import CHANNEL_ALIASES, load_ld_data, session_segment_diagnostics
from ghost_car.track_surface import TrackSurface


class BundledDependencyTest(unittest.TestCase):
    def test_default_motec_parser_is_vendored(self):
        self.assertEqual(load_ld_data().__module__, "ghost_car._vendor.ldparser")


class SessionSegmentDiagnosticsTest(unittest.TestCase):
    def test_labels_out_lap_timed_laps_and_in_lap(self):
        segments = session_segment_diagnostics(
            20.0,
            [
                {"lap": 1, "startS": 5.0, "endS": 10.0, "durationS": 5.0},
                {"lap": 2, "startS": 10.0, "endS": 15.0, "durationS": 5.0},
            ],
        )

        self.assertEqual(
            segments,
            [
                {"kind": "out-lap", "startS": 0.0, "endS": 5.0, "durationS": 5.0},
                {
                    "kind": "timed-lap",
                    "lap": 1,
                    "startS": 5.0,
                    "endS": 10.0,
                    "durationS": 5.0,
                },
                {
                    "kind": "timed-lap",
                    "lap": 2,
                    "startS": 10.0,
                    "endS": 15.0,
                    "durationS": 5.0,
                },
                {"kind": "in-lap", "startS": 15.0, "endS": 20.0, "durationS": 5.0},
            ],
        )

    def test_normalizes_ldx_markers_to_nonzero_running_time_origin(self):
        segments = session_segment_diagnostics(
            20.0,
            [{"lap": 1, "startS": 105.0, "endS": 115.0, "durationS": 10.0}],
            time_origin_s=100.0,
        )

        self.assertEqual(segments[0]["kind"], "out-lap")
        self.assertEqual(segments[0]["durationS"], 5.0)
        self.assertEqual(segments[1]["kind"], "timed-lap")
        self.assertEqual(segments[1]["startS"], 5.0)
        self.assertEqual(segments[2]["kind"], "in-lap")
        self.assertEqual(segments[2]["durationS"], 5.0)

    def test_returns_no_classification_without_lap_markers(self):
        self.assertEqual(session_segment_diagnostics(20.0, []), [])


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

    def test_kn5_mode_uses_surface_height_plus_native_body_clearance(self):
        surface = TrackSurface(
            [
                [-10.0, 2.0, -10.0],
                [20.0, 2.0, -10.0],
                [-10.0, 2.0, 20.0],
                [20.0, 2.0, 20.0],
            ],
            [[0, 1, 2], [1, 3, 2]],
        )
        reference = self.reference.copy()
        reference[:, 1] = 2.5

        aligned, diagnostics = align_replay_heights(
            self.mapped,
            reference,
            mode="kn5",
            track_surface=surface,
        )

        np.testing.assert_allclose(aligned[:, 1], 2.5)
        self.assertAlmostEqual(diagnostics["surfaceBodyClearanceM"], 0.5)
        self.assertEqual(diagnostics["surfaceMatchRatio"], 1.0)


class ReplayPoseTest(unittest.TestCase):
    def test_actual_time_comparison_holds_faster_car_after_finish(self):
        poses = [
            {
                "positionM": [1.0, 2.0, 3.0],
                "velocityMS": [4.0, 0.0, 5.0],
                "gas": 100,
                "brake": 20,
                "currentLapTimeMs": 15000,
            }
        ]

        padded = _pad_comparison_poses(poses, 3)

        self.assertEqual(len(padded), 3)
        self.assertEqual(padded[-1]["positionM"], [1.0, 2.0, 3.0])
        self.assertEqual(padded[-1]["velocityMS"], [0.0, 0.0, 0.0])
        self.assertEqual(padded[-1]["gas"], 0)
        self.assertEqual(padded[-1]["brake"], 0)
        self.assertEqual(padded[-1]["currentLapTimeMs"], 15000)
        self.assertEqual(poses[0]["velocityMS"], [4.0, 0.0, 5.0])

    def test_lap_progress_sync_uses_cumulative_distance(self):
        points = [
            {"timeS": 0.0, "speedMS": 4.0, "lapNumber": 3},
            {"timeS": 1.0, "speedMS": 4.0, "lapNumber": 3},
            {"timeS": 2.0, "speedMS": 4.0, "lapNumber": 3},
            {"timeS": 3.0, "speedMS": 4.0, "lapNumber": 3},
        ]
        xyz = np.array(
            [
                [0.0, 0.0, 0.0],
                [1.0, 0.0, 0.0],
                [4.0, 0.0, 0.0],
                [4.0, 0.0, 6.0],
            ]
        )

        synced, synced_xyz, diagnostics = _synchronize_lap_progress(
            points, xyz, duration_s=20.0
        )

        np.testing.assert_allclose(
            [point["timeS"] for point in synced], [0.0, 2.0, 8.0, 20.0]
        )
        self.assertTrue(all(point["speedMS"] == 0.5 for point in synced))
        self.assertTrue(all(point["lapNumber"] == 1 for point in synced))
        np.testing.assert_allclose(synced_xyz, xyz)
        self.assertAlmostEqual(diagnostics["comparisonPathLengthM"], 10.0)

    def test_lap_progress_sync_uses_shared_track_station_when_available(self):
        points = [
            {"timeS": 0.0, "speedMS": 4.0, "lapNumber": 3},
            {"timeS": 1.0, "speedMS": 4.0, "lapNumber": 3},
            {"timeS": 2.0, "speedMS": 4.0, "lapNumber": 3},
        ]
        xyz = np.array(
            [
                [0.0, 0.0, 0.0],
                [4.0, 0.0, 2.0],
                [10.0, 0.0, 0.0],
            ]
        )
        reference = np.array(
            [
                [0.0, 0.0, 0.0],
                [10.0, 0.0, 0.0],
                [10.0, 0.0, 10.0],
                [0.0, 0.0, 10.0],
            ]
        )

        synced, _, diagnostics = _synchronize_lap_progress(
            points, xyz, duration_s=10.0, reference_xyz=reference
        )

        self.assertAlmostEqual(synced[1]["timeS"], 4.0)
        self.assertEqual(diagnostics["comparisonSynchronization"], "track-progress")

    def test_pose_builder_resets_lap_time_across_full_session(self):
        points = []
        for index in range(6):
            points.append(
                {
                    "timeS": index * 0.03,
                    "lapNumber": 1 if index < 3 else 2,
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
        xyz = np.column_stack(
            (np.zeros(6), np.zeros(6), np.arange(6, dtype=float))
        )

        poses, _ = build_poses_from_xyz(points, xyz, np.eye(2))

        laps = np.asarray([pose["currentLap"] for pose in poses])
        lap_time = np.asarray([pose["currentLapTimeMs"] for pose in poses])
        transition = int(np.flatnonzero(np.diff(laps))[0] + 1)
        lap_boundary = np.asarray(
            [pose["lapBoundaryPulse"] for pose in poses], dtype=bool
        )
        self.assertEqual(laps[transition], 1)
        self.assertLessEqual(lap_time[transition], 15)
        self.assertGreater(lap_time[transition - 1], lap_time[transition])
        self.assertEqual(np.flatnonzero(lap_boundary).tolist(), [transition])

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

    def test_track_lateral_filter_preserves_longitudinal_and_height(self):
        reference = np.column_stack(
            (np.zeros(41), np.zeros(41), np.arange(41, dtype=float))
        )
        positions = reference.copy()
        positions[:, 0] = 0.2 * ((-1.0) ** np.arange(41))
        positions[:, 1] = np.linspace(3.0, 4.0, 41)
        smoothed, diagnostics = smooth_replay_positions(
            positions, frequency_hz=20.0, window_s=0.45, reference_xyz=reference
        )
        np.testing.assert_allclose(smoothed[:, 2], positions[:, 2])
        np.testing.assert_allclose(smoothed[:, 1], positions[:, 1])
        self.assertLess(np.std(smoothed[:, 0]), np.std(positions[:, 0]) * 0.5)
        self.assertEqual(diagnostics["positionSmoothingMode"], "track-lateral")
        self.assertEqual(diagnostics["trackLateralSmoothingSamples"], 9)
        self.assertGreater(diagnostics["trackLateralAdaptiveWeightMean"], 0.0)


class ReplayTrackValidationTest(unittest.TestCase):
    @staticmethod
    def _surface_resolution_fixture():
        replay = {
            "header": {"track": "example_circuit", "trackConfig": "layout_a"},
            "cars": [{"frames": [{"positionM": [0.0, 0.0, 0.0]}]}],
        }
        track_ref = {
            "track": {"name": "example_circuit", "layout": "layout_a"},
            "referencePathAc": [[0.0, 0.0, 0.0]],
        }
        return replay, track_ref

    def test_track_mode_does_not_require_unbundled_surface(self):
        replay, track_ref = self._surface_resolution_fixture()
        with patch(
            "ghost_car.ld_replay.extract_motec_points", return_value={}
        ), patch(
            "ghost_car.ld_replay.parse_acreplay", return_value=replay
        ), patch(
            "ghost_car.ld_replay.load_track_package",
            return_value=(track_ref, Path("track.json"), [Path("missing.gcsurface")], {}),
        ), patch(
            "ghost_car.ld_replay.TrackSurface.from_files"
        ) as load_surface, patch(
            "ghost_car.ld_replay.gps_track_to_ac",
            side_effect=RuntimeError("mapping reached"),
        ):
            with self.assertRaisesRegex(RuntimeError, "mapping reached"):
                convert_ld_to_acreplay(
                    "template.acreplay",
                    "session.ld",
                    "output.acreplay",
                    gps_track_path="track-package",
                    height_mode="track",
                )
        load_surface.assert_not_called()

    def test_kn5_mode_explains_how_to_generate_unbundled_surface(self):
        replay, track_ref = self._surface_resolution_fixture()
        with patch(
            "ghost_car.ld_replay.extract_motec_points", return_value={}
        ), patch(
            "ghost_car.ld_replay.parse_acreplay", return_value=replay
        ), patch(
            "ghost_car.ld_replay.load_track_package",
            return_value=(track_ref, Path("track.json"), [Path("missing.gcsurface")], {}),
        ):
            with self.assertRaisesRegex(ValueError, "export-kn5-surface"):
                convert_ld_to_acreplay(
                    "template.acreplay",
                    "session.ld",
                    "output.acreplay",
                    gps_track_path="track-package",
                    height_mode="kn5",
                )

    def test_rejects_unknown_comparison_timing(self):
        with self.assertRaisesRegex(ValueError, "comparison timing"):
            convert_ld_to_acreplay(
                "template.acreplay",
                "session.ld",
                "comparison.acreplay",
                compare_laps=[1, 2],
                compare_sync="unknown",
                gps_track_path="track.json",
            )

    def test_track_calibration_offset_only_moves_transform_translation(self):
        track_ref = {
            "origin": {"latitudeDeg": 1.0, "longitudeDeg": 2.0},
            "enuToAc": {
                "matrix": [
                    [1.0, 0.0, 0.0, 10.0],
                    [0.0, 0.0, 1.0, 20.0],
                    [0.0, -1.0, 0.0, 30.0],
                    [0.0, 0.0, 0.0, 1.0],
                ]
            },
            "referencePathAc": [[1.0, 2.0, 3.0]],
            "calibration": {"method": "trajectory-shape-fit"},
        }

        adjusted = offset_track_calibration(track_ref, x_m=0.5, z_m=-0.25)

        self.assertEqual(adjusted["enuToAc"]["matrix"][0][3], 10.5)
        self.assertEqual(adjusted["enuToAc"]["matrix"][2][3], 29.75)
        self.assertEqual(adjusted["referencePathAc"], track_ref["referencePathAc"])
        self.assertEqual(
            adjusted["calibration"]["manualOffsetAcM"],
            {"x": 0.5, "z": -0.25},
        )
        self.assertEqual(track_ref["enuToAc"]["matrix"][0][3], 10.0)

    def test_track_calibration_offsets_accumulate(self):
        track_ref = {
            "enuToAc": {
                "matrix": [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0],
                ]
            },
            "calibration": {"manualOffsetAcM": {"x": 1.0, "z": 2.0}},
        }

        adjusted = offset_track_calibration(track_ref, x_m=-0.25, z_m=0.5)

        self.assertEqual(
            adjusted["calibration"]["manualOffsetAcM"],
            {"x": 0.75, "z": 2.5},
        )

    def test_gps_mapping_offsets_lap_origin_from_track_origin(self):
        earth_radius = 6371008.8
        east_m = 10.0
        points = {
            "origin": {
                "latitudeDeg": 0.0,
                "longitudeDeg": math.degrees(east_m / earth_radius),
                "altitudeM": 0.0,
            },
            "points": [{"xM": 0.0, "yM": 0.0, "zM": 0.0}],
        }
        track_ref = {
            "origin": {
                "latitudeDeg": 0.0,
                "longitudeDeg": 0.0,
                "altitudeM": 0.0,
            },
            "enuToAc": {
                "matrix": [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0],
                ]
            },
            "referencePathAc": [[east_m, 0.0, 0.0]],
        }

        mapped, rms, rotation = gps_track_to_ac(points, track_ref)

        np.testing.assert_allclose(mapped[0], [east_m, 0.0, 0.0], atol=1e-6)
        np.testing.assert_allclose(rotation, np.eye(2))
        self.assertAlmostEqual(rms, 0.0, places=6)

    def test_comparison_requires_calibrated_track(self):
        with self.assertRaisesRegex(ValueError, "--gps-track"):
            convert_ld_to_acreplay(
                "template.acreplay",
                "session.ld",
                "comparison.acreplay",
                compare_laps=[1, 2],
            )

    def test_session_and_comparison_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            convert_ld_to_acreplay(
                "template.acreplay",
                "session.ld",
                "comparison.acreplay",
                session=True,
                compare_laps=[1, 2],
                gps_track_path="track.json",
            )

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
