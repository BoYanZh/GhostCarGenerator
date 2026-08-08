"""Codec for iRacing BLAP and OLAP files."""

import base64
import math
import os
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


TABLE_HEADER = 0x5B0
TABLE_RECORDS = 0x5C0
TABLE_RECORD_SIZE = 0x20
SAMPLE_SIZE = 0x1C
SAMPLE = struct.Struct("<ffffffI")
BinarySource = Union[str, os.PathLike, bytes, bytearray]


def read_bytes(source: BinarySource) -> bytes:
    if isinstance(source, (str, os.PathLike)):
        return Path(source).read_bytes()
    return bytes(source)


def parse_int(value: Union[str, int]) -> int:
    return value if isinstance(value, int) else int(value, 0)


def decode_fixed(raw: bytes, offset: int, size: int) -> str:
    return raw[offset:offset + size].split(b"\x00", 1)[0].decode("latin-1")


def write_fixed(buf: bytearray, offset: int, size: int, value: str) -> None:
    encoded = value.encode("latin-1")
    if len(encoded) >= size:
        raise ValueError("{}-byte field cannot hold {!r}".format(size, value))
    buf[offset:offset + size] = encoded + b"\x00" * (size - len(encoded))


def detect_blap_body_offset(raw: bytes, override: Optional[int] = None) -> int:
    if override is None:
        if len(raw) < TABLE_RECORDS:
            raise ValueError("Lapfile is too small to contain a v3 sector table")
        count = struct.unpack_from("<I", raw, TABLE_HEADER + 12)[0]
        offset = TABLE_RECORDS + count * TABLE_RECORD_SIZE
    else:
        offset = int(override)
    if offset < TABLE_RECORDS or offset > len(raw):
        raise ValueError("Invalid BLAP body offset 0x{:X}".format(offset))
    if (len(raw) - offset) % SAMPLE_SIZE:
        raise ValueError(
            "Body length at 0x{:X} is not divisible by {}".format(offset, SAMPLE_SIZE)
        )
    return offset


def parse_sectors(raw: bytes, count: int) -> List[Dict[str, Any]]:
    result = []
    for index in range(count):
        offset = TABLE_RECORDS + index * TABLE_RECORD_SIZE
        values = struct.unpack_from("<ffIffffI", raw, offset)
        start, end, bins, spacing, unknown_1, unknown_2, best_time, flags = values
        result.append(
            {
                "sectorIndex": index + 1,
                "startDistanceM": start,
                "endDistanceM": end,
                "sectorLengthM": end - start,
                "numBins": bins,
                "sampleSpacingM": spacing,
                "unknownFloat1": unknown_1,
                "unknownFloat2": unknown_2,
                "sectorBestTimeS": best_time,
                "recordFlags": flags,
            }
        )
    return result


def parse_blap(
    source: BinarySource,
    body_offset: Optional[int] = None,
    include_prefix: bool = True,
    brake_light_threshold: int = 10,
) -> Dict[str, Any]:
    raw = read_bytes(source)
    body_offset = detect_blap_body_offset(raw, body_offset)
    record_count = (body_offset - TABLE_RECORDS) // TABLE_RECORD_SIZE
    sectors = parse_sectors(raw, record_count)
    total_bins = (len(raw) - body_offset) // SAMPLE_SIZE
    samples = []
    x_m = y_m = z_m = 0.0
    sector_index = 0
    bin_in_sector = 0
    for index in range(total_bins):
        offset = body_offset + index * SAMPLE_SIZE
        time_s, delta_s, yaw, pitch, roll, reserved, flags = SAMPLE.unpack_from(raw, offset)
        while (
            sector_index + 1 < len(sectors)
            and bin_in_sector >= sectors[sector_index]["numBins"]
        ):
            sector_index += 1
            bin_in_sector = 0
        spacing = sectors[sector_index]["sampleSpacingM"] if sectors else 0.0
        horizontal = spacing * math.cos(pitch)
        x_m += horizontal * math.cos(yaw)
        y_m += horizontal * math.sin(yaw)
        z_m += spacing * math.sin(pitch)
        brake_raw = (flags >> 8) & 0xFF
        samples.append(
            {
                "globalBinIndex": index,
                "sectorIndex": sector_index + 1 if sectors else 0,
                "timeInSectorS": time_s,
                "deltaS": delta_s,
                "yawRad": yaw,
                "pitchRad": pitch,
                "rollRad": roll,
                "reserved": reserved,
                "flags": flags,
                "gear": (flags >> 24) & 0xFF,
                "systemRaw": (flags >> 16) & 0xFF,
                "brakeRaw": brake_raw,
                "throttleRaw": flags & 0xFF,
                "brakeLightOn": brake_raw > brake_light_threshold,
                "xM": x_m,
                "yM": y_m,
                "zM": z_m,
            }
        )
        bin_in_sector += 1
    result = {
        "header": {
            "magic": raw[:4].decode("ascii", errors="replace"),
            "version": struct.unpack_from("<I", raw, 4)[0],
            "flags": struct.unpack_from("<I", raw, 8)[0],
            "custId": struct.unpack_from("<I", raw, 12)[0],
            "driverName": decode_fixed(raw, 0x10, 64),
            "carShortName": decode_fixed(raw, 0x90, 64),
            "trackName": decode_fixed(raw, 0x53E, 64),
            "buildDates": [
                decode_fixed(raw, offset, 16)
                for offset in (0x57E, 0x58E, 0x59E)
            ],
        },
        "summary": {
            "bestLapS": struct.unpack_from("<f", raw, TABLE_HEADER + 4)[0],
            "tableTypeCount": struct.unpack_from("<I", raw, TABLE_HEADER + 8)[0],
            "sectorCount": record_count,
            "totalTrackLengthM": sectors[-1]["endDistanceM"] if sectors else 0.0,
            "totalBins": total_bins,
            "sectors": sectors,
        },
        "samples": samples,
        "binary": {
            "bodyStartOffset": body_offset,
            "sampleSize": SAMPLE_SIZE,
            "tableRecordSize": TABLE_RECORD_SIZE,
        },
    }
    if include_prefix:
        result["binary"]["prefixBase64"] = base64.b64encode(
            raw[:body_offset]
        ).decode("ascii")
    return result


def value(mapping: Dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def patch_header(prefix: bytearray, header: Dict[str, Any]) -> None:
    if "magic" in header:
        magic = str(header["magic"]).encode("ascii")
        if len(magic) != 4:
            raise ValueError("BLAP magic must contain exactly four ASCII bytes")
        if prefix[:4] != magic:
            prefix[:4] = magic
    for name, offset in (("version", 4), ("flags", 8), ("custId", 12)):
        if name in header:
            number = int(header[name]) & 0xFFFFFFFF
            if struct.unpack_from("<I", prefix, offset)[0] != number:
                struct.pack_into("<I", prefix, offset, number)
    for name, offset, size in (
        ("driverName", 0x10, 64),
        ("carShortName", 0x90, 64),
        ("trackName", 0x53E, 64),
    ):
        if name in header and decode_fixed(prefix, offset, size) != str(header[name]):
            write_fixed(prefix, offset, size, str(header[name]))
    if "buildDates" in header:
        dates = list(header["buildDates"])
        if len(dates) > 3:
            raise ValueError("BLAP supports at most three build-date fields")
        for date, offset in zip(dates, (0x57E, 0x58E, 0x59E)):
            if decode_fixed(prefix, offset, 16) != str(date):
                write_fixed(prefix, offset, 16, str(date))


def patch_summary(prefix: bytearray, summary: Dict[str, Any]) -> None:
    record_count = struct.unpack_from("<I", prefix, TABLE_HEADER + 12)[0]
    sectors = summary.get("sectors")
    if sectors is not None and len(sectors) != record_count:
        raise ValueError(
            "JSON has {} sectors but the binary prefix has {}".format(
                len(sectors), record_count
            )
        )
    if "bestLapS" in summary:
        number = float(summary["bestLapS"])
        if struct.unpack_from("<f", prefix, TABLE_HEADER + 4)[0] != number:
            struct.pack_into("<f", prefix, TABLE_HEADER + 4, number)
    if "tableTypeCount" in summary:
        number = int(summary["tableTypeCount"])
        if struct.unpack_from("<I", prefix, TABLE_HEADER + 8)[0] != number:
            struct.pack_into("<I", prefix, TABLE_HEADER + 8, number)
    if sectors is None:
        return
    previous_end = 0.0
    for index, sector in enumerate(sectors):
        offset = TABLE_RECORDS + index * TABLE_RECORD_SIZE
        current = struct.unpack_from("<ffIffffI", prefix, offset)
        start = float(value(sector, "startDistanceM", "start_distance_m", default=previous_end))
        end = float(value(sector, "endDistanceM", "end_distance_m"))
        bins = int(value(sector, "numBins", "num_bins"))
        updated = (
            start,
            end,
            bins,
            float(
                value(
                    sector,
                    "sampleSpacingM",
                    "sample_spacing_m",
                    default=(end - start) / max(1, bins - 1),
                )
            ),
            float(value(sector, "unknownFloat1", default=current[4])),
            float(value(sector, "unknownFloat2", default=current[5])),
            float(
                value(
                    sector,
                    "sectorBestTimeS",
                    "sector_best_time_s",
                    default=current[6],
                )
            ),
            int(value(sector, "recordFlags", default=current[7])),
        )
        if current != updated:
            struct.pack_into("<ffIffffI", prefix, offset, *updated)
        previous_end = end


def get_prefix(
    data: Dict[str, Any],
    template_path: Optional[Union[str, os.PathLike]],
    body_offset: Optional[int],
) -> Tuple[bytearray, int]:
    if template_path is not None:
        template = Path(template_path).read_bytes()
        resolved = detect_blap_body_offset(template, body_offset)
        return bytearray(template[:resolved]), resolved
    binary = data.get("binary", {})
    encoded = binary.get("prefixBase64")
    if not encoded:
        raise ValueError(
            "No embedded prefix; provide --template or decode without --omit-prefix"
        )
    prefix = bytearray(base64.b64decode(encoded, validate=True))
    resolved = body_offset
    if resolved is None:
        resolved = binary.get("bodyStartOffset", len(prefix))
    resolved = int(resolved)
    if len(prefix) != resolved:
        raise ValueError(
            "Embedded prefix length {} does not match body offset 0x{:X}".format(
                len(prefix), resolved
            )
        )
    return prefix, resolved


def pack_blap(
    data: Dict[str, Any],
    template_path: Optional[Union[str, os.PathLike]] = None,
    body_offset: Optional[int] = None,
    default_system_raw: int = 0xFF,
) -> bytes:
    prefix, resolved = get_prefix(data, template_path, body_offset)
    patch_header(prefix, data.get("header", {}))
    patch_summary(prefix, data.get("summary", {}))
    if len(prefix) != resolved:
        raise ValueError("Body offset does not equal prefix length")
    samples = data.get("samples", [])
    sectors = data.get("summary", {}).get("sectors", [])
    if sectors:
        expected = sum(int(value(sector, "numBins", "num_bins")) for sector in sectors)
        if expected != len(samples):
            raise ValueError(
                "Sector table declares {} bins but JSON has {} samples".format(
                    expected, len(samples)
                )
            )
    body = bytearray(len(samples) * SAMPLE_SIZE)
    for index, sample in enumerate(samples):
        flags = sample.get("flags")
        if flags is None:
            gear = int(value(sample, "gear", default=0)) & 0xFF
            system = int(
                value(sample, "systemRaw", "system_raw", default=default_system_raw)
            ) & 0xFF
            brake = int(value(sample, "brakeRaw", "brake_raw", default=0)) & 0xFF
            throttle = int(
                value(sample, "throttleRaw", "throttle_raw", default=0)
            ) & 0xFF
            flags = (gear << 24) | (system << 16) | (brake << 8) | throttle
        SAMPLE.pack_into(
            body,
            index * SAMPLE_SIZE,
            float(value(sample, "timeInSectorS", "time_in_sector_s", default=0.0)),
            float(value(sample, "deltaS", "delta_s", default=0.0)),
            float(value(sample, "yawRad", "yaw_rad", default=0.0)),
            float(value(sample, "pitchRad", "pitch_rad", default=0.0)),
            float(value(sample, "rollRad", "roll_rad", default=0.0)),
            float(sample.get("reserved", 0.0)),
            int(flags) & 0xFFFFFFFF,
        )
    return bytes(prefix + body)
