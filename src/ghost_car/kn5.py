"""Minimal, dependency-free Assetto Corsa KN5 geometry reader.

Only data needed for road-height matching is retained. Textures and materials
are skipped; mesh positions, indices, and node transforms are decoded.
"""
from __future__ import annotations

__all__ = ["export_kn5_surface", "read_kn5_surface"]

import re
import struct
from pathlib import Path

import numpy as np

from .track_surface import TrackSurface

KN5_MAGIC = b"sc6969"
MAX_COUNT = 100_000_000


class _Kn5Reader:
    def __init__(self, handle):
        self.handle = handle
        self.version = 0
        current = handle.tell()
        handle.seek(0, 2)
        self.size = handle.tell()
        handle.seek(current)

    def _read_bytes(self, size, description):
        if size < 0:
            raise ValueError("Negative KN5 {} size".format(description))
        data = self.handle.read(size)
        if len(data) != size:
            raise ValueError("Truncated KN5 {}".format(description))
        return data

    def _read(self, fmt, description):
        parser = struct.Struct(fmt)
        return parser.unpack(self._read_bytes(parser.size, description))

    def _u8(self, description):
        return self._read("<B", description)[0]

    def _u32(self, description):
        return self._read("<I", description)[0]

    def _i32(self, description):
        return self._read("<i", description)[0]

    def _count(self, description):
        value = self._u32(description)
        if value > MAX_COUNT:
            raise ValueError("Unreasonable KN5 {}: {}".format(description, value))
        return value

    def _skip(self, size, description):
        if size < 0 or self.handle.tell() + size > self.size:
            raise ValueError("Truncated KN5 {}".format(description))
        self.handle.seek(size, 1)

    def _string(self, description):
        length = self._count(description + " length")
        data = self._read_bytes(length, description)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Invalid UTF-8 in KN5 {}".format(description)) from error

    def _skip_header_assets(self):
        magic = self._read_bytes(6, "magic")
        if magic != KN5_MAGIC:
            raise ValueError("Not an Assetto Corsa KN5 file")
        self.version = self._u32("version")
        if self.version < 1 or self.version > 6:
            raise ValueError("Unsupported KN5 version {}".format(self.version))
        if self.version > 5:
            self._skip(4, "version-6 header extension")

        for _ in range(self._count("texture count")):
            self._skip(4, "texture active flag")
            self._string("texture name")
            self._skip(self._count("texture byte count"), "texture payload")

        for _ in range(self._count("material count")):
            self._string("material name")
            self._string("material shader")
            self._skip(6, "material modes")
            for _ in range(self._count("material property count")):
                self._string("material property name")
                self._skip(40, "material property values")
            for _ in range(self._count("material mapping count")):
                self._string("material mapping name")
                self._skip(4, "material mapping slot")
                self._string("material texture name")

    @staticmethod
    def _world_positions(vertices, transform):
        return vertices @ transform[:3, :3] + transform[3, :3]

    def _vertices(self, count, stride_bytes, description):
        raw = self._read_bytes(count * stride_bytes, description)
        stride_floats = stride_bytes // 4
        values = np.frombuffer(raw, dtype="<f4").reshape((count, stride_floats))
        return values[:, :3].astype(np.float64, copy=True)

    def _indices(self, description):
        count = self._count(description + " count")
        raw = self._read_bytes(count * 2, description)
        return np.frombuffer(raw, dtype="<u2").astype(np.int64)

    def _node(self, parent_transform, parent_active, matcher, selected, names):
        node_type = self._i32("node type")
        if node_type not in (1, 2, 3):
            raise ValueError("Unsupported KN5 node type {}".format(node_type))
        name = self._string("node name")
        child_count = self._count("node child count")
        active = bool(self._u8("node active flag")) and parent_active
        transform = parent_transform

        if node_type == 1:
            local = np.asarray(
                self._read("<16f", "base-node transform"), dtype=np.float64
            ).reshape((4, 4))
            transform = local @ parent_transform
        else:
            self._skip(3, "mesh visibility flags")
            if node_type == 3:
                for _ in range(self._count("bone count")):
                    self._string("bone name")
                    self._skip(64, "bone transform")
            vertex_count = self._count("mesh vertex count")
            vertices = self._vertices(
                vertex_count,
                44 if node_type == 2 else 76,
                "mesh vertices",
            )
            indices = self._indices("mesh indices")
            self._skip(16, "mesh material/layer/LOD")
            if node_type == 2:
                self._skip(16, "mesh bounding sphere")
                renderable = bool(self._u8("mesh renderable flag"))
            else:
                renderable = True
            if len(indices) % 3:
                raise ValueError(
                    "KN5 mesh {!r} index count is not divisible by 3".format(name)
                )
            if len(indices) and int(indices.max()) >= vertex_count:
                raise ValueError("KN5 mesh {!r} has an invalid vertex index".format(name))
            if active and renderable and matcher.search(name) and len(indices):
                selected.append(
                    (
                        self._world_positions(vertices, transform),
                        indices.reshape((-1, 3)),
                    )
                )
                names.append(name)

        for _ in range(child_count):
            self._node(transform, active, matcher, selected, names)

    def read_surface(self, mesh_pattern):
        self._skip_header_assets()
        try:
            matcher = re.compile(mesh_pattern, re.IGNORECASE)
        except re.error as error:
            raise ValueError("Invalid mesh pattern: {}".format(error)) from error
        selected = []
        names = []
        self._node(np.eye(4, dtype=np.float64), True, matcher, selected, names)
        if not selected:
            raise ValueError("No renderable KN5 meshes matched {!r}".format(mesh_pattern))
        vertices = []
        triangles = []
        offset = 0
        for mesh_vertices, mesh_triangles in selected:
            vertices.append(mesh_vertices)
            triangles.append(mesh_triangles + offset)
            offset += len(mesh_vertices)
        return TrackSurface(np.vstack(vertices), np.vstack(triangles)), names


def read_kn5_surface(path, mesh_pattern=r"^(1ROAD|1PIT)"):
    """Read matching KN5 meshes into one world-coordinate TrackSurface."""
    path = Path(path).expanduser()
    with path.open("rb") as handle:
        return _Kn5Reader(handle).read_surface(mesh_pattern)


def export_kn5_surface(kn5_path, output_path, mesh_pattern=r"^(1ROAD|1PIT)"):
    """Extract matching KN5 geometry and write a .gcsurface file."""
    surface, names = read_kn5_surface(kn5_path, mesh_pattern=mesh_pattern)
    output = surface.to_file(output_path)
    return {
        "output": str(output),
        "meshNames": names,
        "meshCount": len(names),
        "vertexCount": len(surface.vertices),
        "triangleCount": len(surface.triangles),
    }
