"""Minimal dependency-free text-to-PDF writer (for generated cover letters)."""

from __future__ import annotations

import textwrap


def _esc(text: str) -> str:
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_to_pdf(text: str, *, font_size: int = 11, width_chars: int = 92, lines_per_page: int = 58) -> bytes:
    lines: list[str] = []
    for para in text.replace("\r\n", "\n").split("\n"):
        lines += textwrap.wrap(para, width_chars) or [""]
    pages = [lines[i:i + lines_per_page] for i in range(0, max(1, len(lines)), lines_per_page)] or [[""]]
    objects: list[bytes] = []
    kids = []
    font_obj = 3 + 2 * len(pages)
    for idx, page in enumerate(pages):
        page_obj, content_obj = 3 + 2 * idx, 4 + 2 * idx
        kids.append(f"{page_obj} 0 R")
        stream = ["BT", f"/F1 {font_size} Tf", f"{int(font_size * 1.35)} TL", "56 740 Td"]
        stream += [f"({_esc(line)}) Tj T*" for line in page]
        stream.append("ET")
        body = "\n".join(stream).encode("latin-1")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_obj} 0 R >> >> "
                       f"/Contents {content_obj} 0 R >>".encode())
        objects.append(b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    all_objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
                f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()] + objects
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(all_objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(all_objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(all_objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
