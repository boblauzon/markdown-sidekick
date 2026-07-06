"""Build minimal valid PDFs in-memory for tests — no extra dependencies.

Produces a real multi-page PDF with a Helvetica text layer that pypdfium2 can
parse and extract. Not general-purpose: ASCII text, one text run per page.
"""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf(pages: list[str], image_on_page: int | None = None) -> bytes:
    """Return PDF bytes with one text line per entry in ``pages``.

    ``image_on_page`` (0-based) additionally embeds a 120x100 RGB image
    XObject on that page, so figure extraction can be tested.
    """
    objects: list[bytes] = []  # 1-indexed body objects, in object-number order

    n_pages = len(pages)
    has_image = image_on_page is not None
    # Object numbers: 1 catalog, 2 pages, 3 font, [4 image], then page/content pairs.
    first_page_obj = 5 if has_image else 4
    page_obj_nums = [first_page_obj + 2 * i for i in range(n_pages)]

    kids = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode("ascii")
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    if has_image:
        width, height = 120, 100
        # A simple horizontal gradient so the pixel data isn't degenerate.
        pixels = bytearray()
        for _y in range(height):
            for x in range(width):
                pixels += bytes((x * 2 % 256, 80, 200))
        objects.append(
            (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length {len(pixels)} >>"
            ).encode("ascii")
            + b"\nstream\n"
            + bytes(pixels)
            + b"\nendstream"
        )

    for i, text in enumerate(pages):
        ops = f"BT /F1 24 Tf 72 700 Td ({_escape(text)}) Tj ET"
        resources = "<< /Font << /F1 3 0 R >> >>"
        if has_image and i == image_on_page:
            ops += " q 240 0 0 200 100 380 cm /Im1 Do Q"
            resources = "<< /Font << /F1 3 0 R >> /XObject << /Im1 4 0 R >> >>"
        content = ops.encode("ascii")
        page = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {page_obj_nums[i] + 1} 0 R "
            f"/Resources {resources} >>"
        ).encode("ascii")
        objects.append(page)
        objects.append(
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]  # object 0 is the free-list head
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_pos = len(out)
    total = len(objects) + 1
    out += f"xref\n0 {total}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)
