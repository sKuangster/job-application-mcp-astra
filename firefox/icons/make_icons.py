#!/usr/bin/env python3
"""Generate the ApplyPilot toolbar icons (standard library only).

Draws a white paper-plane glyph on an indigo rounded square, 4x4 supersampled
for anti-aliasing. Usage: python3 firefox/icons/make_icons.py
"""
import os
import struct
import zlib

BG = (79, 70, 229)      # indigo
FG = (255, 255, 255)
HERE = os.path.dirname(os.path.abspath(__file__))

# Paper plane, in unit coordinates (0..1), two triangles (wing + fold).
PLANE = [
    [(0.20, 0.50), (0.82, 0.22), (0.44, 0.60)],
    [(0.44, 0.60), (0.82, 0.22), (0.56, 0.82)],
]
FOLD = [(0.44, 0.60), (0.50, 0.78), (0.53, 0.66)]


def in_tri(px, py, tri):
    (x1, y1), (x2, y2), (x3, y3) = tri
    d1 = (px - x2) * (y1 - y2) - (x1 - x2) * (py - y2)
    d2 = (px - x3) * (y2 - y3) - (x2 - x3) * (py - y3)
    d3 = (px - x1) * (y3 - y1) - (x3 - x1) * (py - y1)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def in_rounded(px, py, r=0.2):
    if not (0 <= px <= 1 and 0 <= py <= 1):
        return False
    cx = min(max(px, r), 1 - r)
    cy = min(max(py, r), 1 - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def render(size, ss=4):
    rows = []
    for y in range(size):
        row = bytearray([0])  # filter type 0
        for x in range(size):
            bg = fg = fold = 0
            for sy in range(ss):
                for sx in range(ss):
                    px = (x + (sx + 0.5) / ss) / size
                    py = (y + (sy + 0.5) / ss) / size
                    if in_rounded(px, py):
                        bg += 1
                        if any(in_tri(px, py, t) for t in PLANE):
                            fg += 1
                            if in_tri(px, py, FOLD):
                                fold += 1
            n = ss * ss
            a = bg / n
            if bg:
                f = fg / bg
                fo = fold / bg
                col = [BG[i] * (1 - f) + FG[i] * f for i in range(3)]
                # fold slightly shaded
                col = [c * (1 - 0.18 * fo) for c in col]
            else:
                col = [0, 0, 0]
            row += bytes([int(round(c)) for c in col] + [int(round(a * 255))])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    for s in (16, 32, 48, 96, 128):
        with open(os.path.join(HERE, f"icon-{s}.png"), "wb") as fh:
            fh.write(render(s))
    print("icons written to", HERE)
