# -*- coding: utf-8 -*-
"""Turn ``packaging/DiskWarden.ico`` into ``packaging/DiskWarden.icns``.

The Windows icon already stores every size as an embedded PNG, so the
conversion is a straight reformat: no image library, no ``iconutil``, and it
runs on any host (the macOS build machine included).  Sizes that the ``.ico``
does not contain are skipped rather than upscaled, so the bundle never ships a
blurry icon entry.

    python packaging/make_icns.py

Python 3.8 compatible.
"""

import os
import struct
import sys

ICNS_MAGIC = b"icns"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Pixel size -> ICNS chunk type.  These are the exact-size PNG slots; the
# "@2x" slots (ic11-ic14) need a doubled image, which the .ico does not have.
CHUNK_TYPES = (
    (16, b"icp4"),
    (32, b"icp5"),
    (64, b"icp6"),
    (128, b"ic07"),
    (256, b"ic08"),
)


def read_ico_entries(data):
    # type: (bytes) -> dict
    """Return ``{pixel_size: png_bytes}`` for PNG-compressed icon entries."""
    if len(data) < 6:
        raise ValueError("not an ICO file")
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    if reserved != 0 or kind != 1:
        raise ValueError("not an ICO file")
    entries = {}
    for index in range(count):
        offset = 6 + 16 * index
        if offset + 16 > len(data):
            raise ValueError("truncated ICO directory")
        width, height, _colors, _reserved, _planes, _bpp, size, start = struct.unpack(
            "<BBBBHHII", data[offset:offset + 16]
        )
        width = width or 256
        height = height or 256
        if width != height:
            continue
        blob = data[start:start + size]
        if blob[:8] == PNG_MAGIC:
            entries[width] = blob
    return entries


def build_icns(entries):
    # type: (dict) -> bytes
    """Wrap the PNG entries in an ICNS container (chunk = type + length + data)."""
    chunks = []
    for size, chunk_type in CHUNK_TYPES:
        blob = entries.get(size)
        if not blob:
            continue
        chunks.append(
            chunk_type + struct.pack(">I", len(blob) + 8) + blob
        )
    if not chunks:
        raise ValueError("no usable icon entries found")
    payload = b"".join(chunks)
    return ICNS_MAGIC + struct.pack(">I", len(payload) + 8) + payload


def convert(ico_path, icns_path):
    # type: (str, str) -> list
    """Write ``icns_path`` and return the chunk types that were written."""
    with open(ico_path, "rb") as stream:
        entries = read_ico_entries(stream.read())
    data = build_icns(entries)
    with open(icns_path, "wb") as stream:
        stream.write(data)
    used = [chunk_type for size, chunk_type in CHUNK_TYPES if size in entries]
    return used


def main(argv=None):
    # type: (object) -> int
    argv = sys.argv[1:] if argv is None else list(argv)
    here = os.path.dirname(os.path.abspath(__file__))
    ico_path = argv[0] if argv else os.path.join(here, "DiskWarden.ico")
    icns_path = (
        argv[1] if len(argv) > 1 else os.path.join(here, "DiskWarden.icns")
    )
    used = convert(ico_path, icns_path)
    print("wrote %s (%s)" % (icns_path, ", ".join(item.decode() for item in used)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
