"""Tests for the corridor-constrained lateral-offset correction."""

import math
import struct
import tempfile
import unittest
from pathlib import Path

from ghost_car.corridor import constrain_blap
from ghost_car.iracing import parse_blap

TABLE_HEADER = 0x5B0
TABLE_RECORDS = 0x5C0
TABLE_RECORD_SIZE = 0x20
SAMPLE_SIZE = 0x1C
SAMPLE = struct.Struct("<ffffffI")


def build_blap(offsets, track="sonoma\\2025\\nascarlong", car="mx5\\mx52016"):
    """Construct a minimal valid v3 BLAP with a single sector."""
    n = len(offsets)
    spacing = 2.0
    track_length = (n - 1) * spacing
    best_lap = 100.0
    prefix = bytearray(TABLE_RECORDS + TABLE_RECORD_SIZE)
    prefix[:4] = b"BLAP"
    struct.pack_into("<I", prefix, 4, 3)
    struct.pack_into("<I", prefix, 8, 0)
    struct.pack_into("<I", prefix, 12, 123456)
    driver = "Test Driver".encode("latin-1")
    prefix[0x10 : 0x10 + len(driver)] = driver
    car_bytes = car.encode("latin-1")
    prefix[0x90 : 0x90 + len(car_bytes)] = car_bytes
    track_bytes = track.encode("latin-1")
    prefix[0x53E : 0x53E + len(track_bytes)] = track_bytes
    for offset, date in ((0x57E, "2026.01.01.01"), (0x58E, "2026.01.01.02")):
        encoded = date.encode("latin-1")
        prefix[offset : offset + len(encoded)] = encoded
    struct.pack_into("<f", prefix, TABLE_HEADER + 4, best_lap)
    struct.pack_into("<I", prefix, TABLE_HEADER + 8, 3)
    struct.pack_into("<I", prefix, TABLE_HEADER + 12, 1)
    sector = (0.0, track_length, n, spacing, 0.0, 0.0, best_lap, 3)
    struct.pack_into("<ffIffffI", prefix, TABLE_RECORDS, *sector)
    body = bytearray()
    for offset in offsets:
        body += SAMPLE.pack(0.0, float(offset), 0.5, 0.0, 0.0, 0.0, 0)
    return bytes(prefix + body)


def read_offsets(path):
    data = parse_blap(str(path), include_prefix=False)
    return [sample["lateralOffsetM"] for sample in data["samples"]]


class ConstrainBlapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

        # Straight narrow corridor: left edge at +10, right edge at -10.
        n = 101
        self.left_offsets = [10.0] * n
        self.right_offsets = [-10.0] * n
        # Source lap: 60 inside, 20 bins poking above the left edge.
        self.source_offsets = [3.0] * n
        for i in range(40, 60):
            self.source_offsets[i] = 15.0

        self.left_path = self.dir / "left.blap"
        self.right_path = self.dir / "right.blap"
        self.source_path = self.dir / "source.blap"
        self.left_path.write_bytes(build_blap(self.left_offsets))
        self.right_path.write_bytes(build_blap(self.right_offsets))
        self.source_path.write_bytes(build_blap(self.source_offsets))

    def tearDown(self):
        self.tmp.cleanup()

    def test_translate_moves_whole_lap(self):
        result = constrain_blap(
            self.source_path, self.left_path, self.right_path, mode="translate"
        )
        diag = result["_diagnostics"]
        self.assertEqual(diag["correctionKind"], "translation")
        # Poking bins need at least -5m to fit inside [+10, -10] + tolerance.
        self.assertLess(diag["afterMaxViolationM"], 0.1)
        self.assertEqual(diag["beforeViolatingBins"], 20)
        corrected = [s["lateralOffsetM"] for s in result["samples"]]
        shift = corrected[0] - self.source_offsets[0]
        # Every bin must move by the same constant.
        for original, value in zip(self.source_offsets, corrected):
            self.assertAlmostEqual(value - original, shift, places=6)
        # Yaw is preserved exactly.
        source_raw = parse_blap(str(self.source_path))
        for before, after in zip(source_raw["samples"], result["samples"]):
            self.assertEqual(before["yawRad"], after["yawRad"])

    def test_clamp_smooths_excursions(self):
        # 2-bin poke above the corridor; clamp must pull it back toward the edge.
        narrow = self.dir / "narrow-source.blap"
        offsets = [3.0] * 101
        offsets[50] = 15.0
        offsets[51] = 15.0
        narrow.write_bytes(build_blap(offsets))
        result = constrain_blap(
            narrow, self.left_path, self.right_path,
            mode="clamp", smooth_bins=3,
        )
        diag = result["_diagnostics"]
        self.assertEqual(diag["correctionKind"], "clamp")
        self.assertLess(diag["afterMaxViolationM"], diag["beforeMaxViolationM"])
        corrected = [s["lateralOffsetM"] for s in result["samples"]]
        # Poke bins are pulled toward the corridor edge.
        self.assertLess(corrected[50], offsets[50])
        # In-clip bins stay untouched.
        self.assertAlmostEqual(corrected[0], offsets[0], places=9)

    def test_mismatched_track_rejected(self):
        other = self.dir / "other-track.blap"
        other.write_bytes(build_blap([0.0] * 101, track="elsewhere\\layout"))
        with self.assertRaises(ValueError):
            constrain_blap(self.source_path, other, self.right_path)

    def test_mismatched_grid_rejected(self):
        other = self.dir / "other-grid.blap"
        other.write_bytes(build_blap([0.0] * 50))
        with self.assertRaises(ValueError):
            constrain_blap(self.source_path, other, self.right_path)

    def test_template_repacks_header(self):
        template_path = self.dir / "template.blap"
        template_path.write_bytes(build_blap([0.0] * 101, car="formulavee"))
        result = constrain_blap(
            self.source_path, self.left_path, self.right_path,
            mode="translate", template_path=str(template_path),
        )
        self.assertEqual(result["header"]["carShortName"], "formulavee")
        # Round-trip the raw output to confirm it parses cleanly.
        raw_path = self.dir / "out.blap"
        raw_path.write_bytes(result["_raw"])
        parsed = parse_blap(str(raw_path))
        self.assertEqual(parsed["header"]["carShortName"], "formulavee")
        self.assertEqual(len(parsed["samples"]), 101)

    def test_round_trip_output(self):
        out_path = self.dir / "out.blap"
        result = constrain_blap(
            self.source_path, self.left_path, self.right_path, mode="translate"
        )
        out_path.write_bytes(result["_raw"])
        parsed = parse_blap(str(out_path))
        self.assertEqual(len(parsed["samples"]), len(self.source_offsets))
        self.assertEqual(parsed["summary"]["totalBins"], 101)


if __name__ == "__main__":
    unittest.main()
