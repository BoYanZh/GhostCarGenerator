"""Road-surface height lookup from geometry extracted from an AC KN5 file."""
from __future__ import annotations

__all__ = ["TrackSurface"]

import math
import struct
from pathlib import Path

import numpy as np

SURFACE_MAGIC = b"GCSURF1\0"


class TrackSurface:
    """Spatially indexed road triangles for vertical X/Z ray matching."""

    def __init__(self, vertices, triangles, cell_size_m=8.0):
        self.vertices = np.asarray(vertices, dtype=np.float64)
        self.triangles = np.asarray(triangles, dtype=np.int64)
        self.cell_size_m = float(cell_size_m)
        if self.vertices.ndim != 2 or self.vertices.shape[1] != 3:
            raise ValueError("Track-surface vertices must be an Nx3 array")
        if self.triangles.ndim != 2 or self.triangles.shape[1] != 3:
            raise ValueError("Track-surface triangles must be an Mx3 array")
        if not np.all(np.isfinite(self.vertices)):
            raise ValueError("Track-surface vertices must be finite")
        if self.cell_size_m <= 0.0:
            raise ValueError("Track-surface cell size must be positive")
        if len(self.triangles) and (
            self.triangles.min() < 0 or self.triangles.max() >= len(self.vertices)
        ):
            raise ValueError("Track-surface triangle index is out of range")
        self._triangle_vertices = self.vertices[self.triangles]
        self._cells = self._build_cells()

    @classmethod
    def from_file(cls, path, cell_size_m=8.0):
        path = Path(path).expanduser()
        with path.open("rb") as handle:
            magic = handle.read(len(SURFACE_MAGIC))
            if magic != SURFACE_MAGIC:
                raise ValueError("Not a GhostCarGenerator KN5 surface file")
            counts = handle.read(8)
            if len(counts) != 8:
                raise ValueError("Truncated track-surface header")
            vertex_count, triangle_count = struct.unpack("<II", counts)
            vertices = np.fromfile(
                handle, dtype="<f4", count=vertex_count * 3
            ).reshape((-1, 3))
            triangles = np.fromfile(
                handle, dtype="<u4", count=triangle_count * 3
            ).reshape((-1, 3))
            if handle.read(1):
                raise ValueError("Track-surface file has unexpected trailing bytes")
        if len(vertices) != vertex_count or len(triangles) != triangle_count:
            raise ValueError("Truncated track-surface geometry")
        return cls(vertices, triangles, cell_size_m=cell_size_m)

    @classmethod
    def combine(cls, surfaces, cell_size_m=8.0):
        surfaces = list(surfaces)
        if not surfaces:
            raise ValueError("At least one track surface is required")
        vertices = []
        triangles = []
        vertex_offset = 0
        for surface in surfaces:
            vertices.append(surface.vertices)
            triangles.append(surface.triangles + vertex_offset)
            vertex_offset += len(surface.vertices)
        return cls(
            np.vstack(vertices),
            np.vstack(triangles),
            cell_size_m=cell_size_m,
        )

    @classmethod
    def from_files(cls, paths, cell_size_m=8.0):
        return cls.combine(
            [cls.from_file(path, cell_size_m=cell_size_m) for path in paths],
            cell_size_m=cell_size_m,
        )

    def to_file(self, path):
        """Write the compact, versioned surface geometry format."""
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        vertices = np.asarray(self.vertices, dtype="<f4")
        triangles = np.asarray(self.triangles, dtype="<u4")
        with path.open("wb") as handle:
            handle.write(SURFACE_MAGIC)
            handle.write(struct.pack("<II", len(vertices), len(triangles)))
            handle.write(vertices.tobytes(order="C"))
            handle.write(triangles.tobytes(order="C"))
        return path

    def _cell(self, x, z):
        return (
            math.floor(float(x) / self.cell_size_m),
            math.floor(float(z) / self.cell_size_m),
        )

    def _build_cells(self):
        cells = {}
        horizontal = self._triangle_vertices[:, :, [0, 2]]
        lower = np.floor(horizontal.min(axis=1) / self.cell_size_m).astype(int)
        upper = np.floor(horizontal.max(axis=1) / self.cell_size_m).astype(int)
        for triangle, (minimum, maximum) in enumerate(zip(lower, upper)):
            for cell_x in range(int(minimum[0]), int(maximum[0]) + 1):
                for cell_z in range(int(minimum[1]), int(maximum[1]) + 1):
                    cells.setdefault((cell_x, cell_z), []).append(triangle)
        return {
            key: np.asarray(value, dtype=np.int64) for key, value in cells.items()
        }

    def sample(self, query_xyz, inside_tolerance=1e-7):
        """Return road Y and unit normals under X/Z, choosing the nearest Y hit."""
        query = np.asarray(query_xyz, dtype=np.float64)
        if query.ndim != 2 or query.shape[1] != 3:
            raise ValueError("Track-surface queries must be an Nx3 array")
        heights = np.full(len(query), np.nan, dtype=np.float64)
        normals = np.full((len(query), 3), np.nan, dtype=np.float64)
        for query_index, point in enumerate(query):
            candidates = self._cells.get(self._cell(point[0], point[2]))
            if candidates is None:
                continue
            triangles = self._triangle_vertices[candidates]
            x1, z1 = triangles[:, 0, 0], triangles[:, 0, 2]
            x2, z2 = triangles[:, 1, 0], triangles[:, 1, 2]
            x3, z3 = triangles[:, 2, 0], triangles[:, 2, 2]
            denominator = (z2 - z3) * (x1 - x3) + (x3 - x2) * (z1 - z3)
            valid = np.abs(denominator) > 1e-12
            first = np.zeros(len(triangles))
            second = np.zeros(len(triangles))
            first[valid] = (
                (z2[valid] - z3[valid]) * (point[0] - x3[valid])
                + (x3[valid] - x2[valid]) * (point[2] - z3[valid])
            ) / denominator[valid]
            second[valid] = (
                (z3[valid] - z1[valid]) * (point[0] - x3[valid])
                + (x1[valid] - x3[valid]) * (point[2] - z3[valid])
            ) / denominator[valid]
            third = 1.0 - first - second
            inside = (
                valid
                & (first >= -inside_tolerance)
                & (second >= -inside_tolerance)
                & (third >= -inside_tolerance)
            )
            if not np.any(inside):
                continue
            hit_triangles = triangles[inside]
            hit_y = (
                first[inside] * hit_triangles[:, 0, 1]
                + second[inside] * hit_triangles[:, 1, 1]
                + third[inside] * hit_triangles[:, 2, 1]
            )
            selected = int(np.argmin(np.abs(hit_y - point[1])))
            heights[query_index] = hit_y[selected]
            edge_a = hit_triangles[selected, 1] - hit_triangles[selected, 0]
            edge_b = hit_triangles[selected, 2] - hit_triangles[selected, 0]
            normal = np.cross(edge_a, edge_b)
            length = float(np.linalg.norm(normal))
            if length > 1e-12:
                normal /= length
                if normal[1] < 0.0:
                    normal *= -1.0
                normals[query_index] = normal
        return heights, normals
