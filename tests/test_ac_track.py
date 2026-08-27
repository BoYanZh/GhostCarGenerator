"""Tests for AC track packages and multi-lap GPS calibration."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ghost_car.ac_track import calibrate_track, load_track_package


class TrackPackageTest(unittest.TestCase):
    def test_rejects_built_in_resource_parent_traversal(self):
        with self.assertRaisesRegex(ValueError, "Invalid built-in track resource"):
            load_track_package("builtin:../private")

    def test_loads_bundled_assetto_corsa_package(self):
        reference, reference_path, surfaces, manifest = load_track_package(
            "builtin:assetto_corsa/thunderhill_raceway_park/threemilebypass"
        )

        self.assertEqual(reference_path.name, "calibration.json")
        self.assertEqual(manifest["trackReference"], "calibration.json")
        self.assertEqual(reference["track"]["name"], "thunderhill_raceway_park")
        self.assertEqual(len(surfaces), 2)

    def test_resolves_reference_and_unbundled_surfaces_from_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "track.json").write_text(
                json.dumps({"referencePathAc": [[0, 0, 0], [1, 0, 0], [0, 0, 1]]}),
                encoding="utf-8",
            )
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "trackReference": "track.json",
                        "surfaces": ["road.gcsurface"],
                    }
                ),
                encoding="utf-8",
            )
            reference, reference_path, surfaces, manifest = load_track_package(package)
            self.assertFalse(surfaces[0].exists())
        self.assertEqual(reference_path.name, "track.json")
        self.assertEqual([item.name for item in surfaces], ["road.gcsurface"])
        self.assertEqual(manifest["trackReference"], "track.json")
        self.assertEqual(len(reference["referencePathAc"]), 3)


class TrackCalibrationTest(unittest.TestCase):
    @staticmethod
    def _point_set(path, noise, selected_lap):
        points = []
        for east, north, up in path:
            points.append(
                {
                    "xM": east + noise,
                    "yM": north - noise * 0.5,
                    "zM": up,
                }
            )
        return {
            "points": points,
            "origin": {
                "latitudeDeg": 37.0,
                "longitudeDeg": -122.0,
                "altitudeM": 100.0,
            },
            "coordinateSystem": "geodetic",
            "selectedLap": selected_lap,
            "selectionSource": "ldx",
            "lapTimeS": 100.0 + selected_lap,
        }

    def test_fits_one_shared_reflected_transform_from_multiple_laps(self):
        source = np.asarray(
            [
                [0.0, 0.0, 0.0],
                [40.0, 0.0, 1.0],
                [55.0, 20.0, 2.0],
                [30.0, 45.0, 3.0],
                [-5.0, 25.0, 1.0],
            ]
        )
        reference = np.column_stack(
            (source[:, 0] + 12.0, source[:, 2] + 4.0, -source[:, 1] + 30.0)
        )
        point_sets = [self._point_set(source, -0.15, 2), self._point_set(source, 0.15, 3)]
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / "one.ld", Path(directory) / "two.ld"]
            reference_path = Path(directory) / "reference.acreplay"
            for path in paths + [reference_path]:
                path.write_bytes(path.name.encode("ascii"))
            loaded_reference = {
                "path": reference,
                "track": {"name": "test_track", "layout": "layout"},
                "sourcePath": reference_path,
                "referenceLap": 1,
                "closureGapM": 0.0,
            }
            with patch(
                "ghost_car.ac_track.extract_motec_points", side_effect=point_sets
            ), patch(
                "ghost_car.ac_track.load_track_reference_path",
                return_value=loaded_reference,
            ):
                result = calibrate_track(
                    reference_path,
                    paths,
                    alignment_samples=100,
                    max_rmse_m=1.0,
                )

        matrix = np.asarray(result["enuToAc"]["matrix"])
        self.assertEqual(result["calibration"]["sourceCount"], 2)
        self.assertTrue(result["calibration"]["reflected"])
        self.assertAlmostEqual(result["calibration"]["scale"], 1.0)
        self.assertLess(result["calibration"]["orderedRmseM"], 0.01)
        self.assertNotIn("referenceSha256", result["calibration"])
        self.assertNotIn("referenceLap", result["calibration"])
        for source_diagnostic in result["calibration"]["sources"]:
            self.assertNotIn("sourceSha256", source_diagnostic)
            self.assertNotIn("selectedLap", source_diagnostic)
            self.assertNotIn("selectionSource", source_diagnostic)
            self.assertNotIn("lapTimeS", source_diagnostic)
        np.testing.assert_allclose(matrix[0, :2], [1.0, 0.0], atol=1e-3)
        np.testing.assert_allclose(matrix[2, :2], [0.0, -1.0], atol=1e-3)
        np.testing.assert_allclose(matrix[[0, 2], 3], [12.0, 30.0], atol=1e-2)


if __name__ == "__main__":
    unittest.main()
