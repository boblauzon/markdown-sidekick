"""Build minimal valid PDFs in-memory for tests — no extra dependencies.

Produces real PDFs that pypdfium2 can parse and extract. ``make_pdf`` is the
simple form (one Helvetica text run per page); ``make_layout_pdf`` places
text runs and images at explicit positions, with optional TrimBox and
bookmark outline, for the layout engine's tests. Text is WinAnsi (latin-1
strings; "\x95" is a bullet) for ``make_layout_pdf``, ASCII for ``make_pdf``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Text:
    """A text run: ``text`` drawn at (x, y) baseline, ``size`` pt.
    ``mono`` uses Courier (a fixed-pitch font) instead of Helvetica."""

    text: str
    x: float
    y: float
    size: float = 10
    mono: bool = False


@dataclass
class Image:
    """An RGB image of ``px`` = (w, h) pixels drawn into ``rect`` = (x, y, w, h)."""

    px: tuple[int, int]
    rect: tuple[float, float, float, float]
    seed: int = 0  # varies the pixel data (content-hash dedupe)


def make_layout_pdf(
    pages: list[list[Text | Image]],
    *,
    size: tuple[float, float] = (612, 792),
    trimbox: tuple[float, float, float, float] | None = None,
    outline: list[tuple[int, str, int]] | None = None,
    title: str | None = None,
) -> bytes:
    """PDF bytes with items placed per page; items draw in the given order
    (so tests control the content-stream order independently of geometry).

    ``outline`` entries are ``(level, title, page_index)``, in document order.
    ``title`` sets the /Info Title.
    """
    objects: dict[int, bytes] = {}
    images = [it for page in pages for it in page if isinstance(it, Image)]
    img_num = {id(it): 5 + k for k, it in enumerate(images)}
    first_page = 5 + len(images)
    page_nums = [first_page + 2 * i for i in range(len(pages))]
    next_num = first_page + 2 * len(pages)

    outline_root = None
    if outline:
        outline_root = next_num
        next_num += 1
        item_nums = [next_num + k for k in range(len(outline))]
        next_num += len(outline)
    info_num = None
    if title:
        info_num = next_num
        next_num += 1

    cat = "<< /Type /Catalog /Pages 2 0 R"
    if outline_root:
        cat += f" /Outlines {outline_root} 0 R /PageMode /UseOutlines"
    objects[1] = (cat + " >>").encode("ascii")
    kids = " ".join(f"{n} 0 R" for n in page_nums)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii")
    # WinAnsi so text can use bytes like 0x95 (bullet) via latin-1 strings.
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    objects[4] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"

    for it in images:
        w, h = it.px
        pixels = bytearray()
        for y in range(h):
            for x in range(w):
                pixels += bytes(((x * 2 + it.seed) % 256, (y + it.seed * 7) % 256, 160))
        objects[img_num[id(it)]] = (
            f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length {len(pixels)} >>"
        ).encode("ascii") + b"\nstream\n" + bytes(pixels) + b"\nendstream"

    for i, items in enumerate(pages):
        ops = []
        xobjs = []
        for it in items:
            if isinstance(it, Text):
                font = "F2" if it.mono else "F1"
                ops.append(f"BT /{font} {it.size} Tf {it.x} {it.y} Td ({_escape(it.text)}) Tj ET")
            else:
                name = f"Im{img_num[id(it)]}"
                x, y, w, h = it.rect
                ops.append(f"q {w} 0 0 {h} {x} {y} cm /{name} Do Q")
                xobjs.append(f"/{name} {img_num[id(it)]} 0 R")
        content = " ".join(ops).encode("latin-1")
        res = "<< /Font << /F1 3 0 R /F2 4 0 R >>"
        if xobjs:
            res += " /XObject << " + " ".join(xobjs) + " >>"
        res += " >>"
        box = f"/MediaBox [0 0 {size[0]} {size[1]}]"
        if trimbox:
            box += " /TrimBox [{} {} {} {}]".format(*trimbox)
        objects[page_nums[i]] = (
            f"<< /Type /Page /Parent 2 0 R {box} "
            f"/Contents {page_nums[i] + 1} 0 R /Resources {res} >>"
        ).encode("ascii")
        objects[page_nums[i] + 1] = b"<< /Length %d >>\nstream\n%s\nendstream" % (
            len(content),
            content,
        )

    if outline_root:
        parents: list[int | None] = []
        stack: list[tuple[int, int]] = []  # (level, item index)
        for k, (level, _t, _p) in enumerate(outline):
            while stack and stack[-1][0] >= level:
                stack.pop()
            parents.append(stack[-1][1] if stack else None)
            stack.append((level, k))

        def children(parent: int | None) -> list[int]:
            return [k for k, p in enumerate(parents) if p == parent]

        for k, (_level, text, page) in enumerate(outline):
            parent = parents[k]
            sibs = children(parent)
            pos = sibs.index(k)
            fields = [
                f"/Title ({_escape(text)})",
                f"/Parent {outline_root if parent is None else item_nums[parent]} 0 R",
                f"/Dest [{page_nums[page]} 0 R /XYZ null null null]",
            ]
            if pos > 0:
                fields.append(f"/Prev {item_nums[sibs[pos - 1]]} 0 R")
            if pos + 1 < len(sibs):
                fields.append(f"/Next {item_nums[sibs[pos + 1]]} 0 R")
            kids_k = children(k)
            if kids_k:
                fields.append(f"/First {item_nums[kids_k[0]]} 0 R /Last {item_nums[kids_k[-1]]} 0 R")
                fields.append(f"/Count {len(kids_k)}")
            objects[item_nums[k]] = ("<< " + " ".join(fields) + " >>").encode("latin-1")
        top = children(None)
        objects[outline_root] = (
            f"<< /Type /Outlines /First {item_nums[top[0]]} 0 R "
            f"/Last {item_nums[top[-1]]} 0 R /Count {len(top)} >>"
        ).encode("ascii")
    if info_num:
        objects[info_num] = f"<< /Title ({_escape(title)}) >>".encode("latin-1")

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode("ascii") + objects[num] + b"\nendobj\n"
    xref_pos = len(out)
    total = max(objects) + 1
    out += f"xref\n0 {total}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for num in range(1, total):
        out += f"{offsets[num]:010d} 00000 n \n".encode("ascii")
    trailer = f"trailer\n<< /Size {total} /Root 1 0 R"
    if info_num:
        trailer += f" /Info {info_num} 0 R"
    out += (trailer + f" >>\nstartxref\n{xref_pos}\n%%EOF\n").encode("ascii")
    return bytes(out)


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf(pages: list[str], image_on_page: int | None = None) -> bytes:
    """Return PDF bytes with one text line per entry in ``pages``.

    ``image_on_page`` (0-based) additionally embeds a 160x130 RGB image
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
        width, height = 160, 130
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
