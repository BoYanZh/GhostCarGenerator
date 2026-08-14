"""Tests for KN5-derived road-surface height matching."""

import tempfile
import unittest
from pathlib import Path

import numpy as np

from ghost_car.track_surface import TrackSurface


class TrackSurfaceTest(unittest.TestCase):
    def test_interpolates_height_and_upward_normal_inside_triangle(self):
        surface = TrackSurface(
            [[0.0, 0.0, 0.0], [2.0, 2.0, 0.0], [0.0, 0.0, 2.0]],
            [[0, 1, 2]],
        )

        heights, normals = surface.sample([[0.5, 10.0, 0.5]])

        self.assertAlmostEqual(heights[0], 0.5)
        self.assertGreater(normals[0, 1], 0.0)
        np.testing.assert_allclose(np.linalg.norm(normals[0]), 1.0)

    def test_selects_vertical_hit_nearest_to_query_height(self):
        vertices = np.asarray(
            [
                [0.0, 0.0, 0.0],
                [2.0, 0.0, 0.0],
                [0.0, 0.0, 2.0],
                [0.0, 10.0, 0.0],
                [2.0, 10.0, 0.0],
                [0.0, 10.0, 2.0],
            ]
        )
        surface = TrackSurface(vertices, [[0, 1, 2], [3, 4, 5]])

        heights, _ = surface.sample([[0.5, 8.0, 0.5], [0.5, 1.0, 0.5]])

        np.testing.assert_allclose(heights, [10.0, 0.0])

    def test_returns_nan_outside_surface(self):
        surface = TrackSurface(
            [[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [0.0, 0.0, 2.0]],
            [[0, 1, 2]],
        )

        heights, normals = surface.sample([[10.0, 0.0, 10.0]])

        self.assertTrue(np.isnan(heights[0]))
        self.assertTrue(np.isnan(normals[0]).all())

    def test_combines_multiple_kn5_surface_files_in_one_coordinate_space(self):
        first = TrackSurface(
            [[0.0, 1.0, 0.0], [2.0, 1.0, 0.0], [0.0, 1.0, 2.0]],
            [[0, 1, 2]],
        )
        second = TrackSurface(
            [[10.0, 3.0, 10.0], [12.0, 3.0, 10.0], [10.0, 3.0, 12.0]],
            [[0, 1, 2]],
        )

        combined = TrackSurface.combine([first, second])
        heights, _ = combined.sample(
            [[0.5, 0.0, 0.5], [10.5, 0.0, 10.5]]
        )

        np.testing.assert_allclose(heights, [1.0, 3.0])

    def test_surface_file_round_trip(self):
        surface = TrackSurface(
            [[0.0, 1.0, 0.0], [2.0, 2.0, 0.0], [0.0, 3.0, 2.0]],
            [[0, 1, 2]],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "surface.gcsurface"
            surface.to_file(path)
            loaded = TrackSurface.from_file(path)
        np.testing.assert_allclose(loaded.vertices, surface.vertices)
        np.testing.assert_array_equal(loaded.triangles, surface.triangles)


if __name__ == "__main__":
    unittest.main()
