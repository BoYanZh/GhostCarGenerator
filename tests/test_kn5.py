"""Tests for dependency-free KN5 geometry extraction."""

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from ghost_car.kn5 import export_kn5_surface, read_kn5_surface
from ghost_car.track_surface import TrackSurface


def _text(value):
    encoded = value.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def _base(name, children, transform):
    return (
        struct.pack("<i", 1)
        + _text(name)
        + struct.pack("<IB", len(children), 1)
        + struct.pack("<16f", *np.asarray(transform).reshape(-1))
        + b"".join(children)
    )


def _mesh(name, positions, indices, renderable=True):
    vertices = b"".join(
        struct.pack("<11f", *position, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        for position in positions
    )
    return (
        struct.pack("<i", 2)
        + _text(name)
        + struct.pack("<IBBBB", 0, 1, 1, 1, 0)
        + struct.pack("<I", len(positions))
        + vertices
        + struct.pack("<I", len(indices))
        + struct.pack("<{}H".format(len(indices)), *indices)
        + struct.pack("<IIff4fB", 0, 0, 0.0, 1000.0, 0.0, 0.0, 0.0, 1.0, renderable)
    )


def _kn5(root):
    return b"sc6969" + struct.pack("<II", 6, 0) + struct.pack("<II", 0, 0) + root


class Kn5SurfaceTest(unittest.TestCase):
    def test_extracts_matching_mesh_and_applies_parent_transform(self):
        transform = np.eye(4)
        transform[3, :3] = [10.0, 2.0, -5.0]
        road = _mesh("1ROAD_TEST", [[0, 0, 0], [2, 0, 0], [0, 0, 2]], [0, 1, 2])
        ignored = _mesh("WALL", [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [0, 1, 2])
        raw = _kn5(_base("ROOT", [_base("PLACED", [road, ignored], transform)], np.eye(4)))
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "track.kn5"
            output_path = Path(directory) / "road.gcsurface"
            input_path.write_bytes(raw)
            result = export_kn5_surface(input_path, output_path)
            loaded = TrackSurface.from_file(output_path)
        self.assertEqual(result["meshNames"], ["1ROAD_TEST"])
        self.assertEqual(result["triangleCount"], 1)
        np.testing.assert_allclose(loaded.vertices[0], [10.0, 2.0, -5.0])

    def test_rejects_file_without_matching_mesh(self):
        raw = _kn5(_base("ROOT", [], np.eye(4)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.kn5"
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "No renderable KN5 meshes"):
                read_kn5_surface(path)


if __name__ == "__main__":
    unittest.main()
