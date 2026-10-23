"""Merge the sheet music of a setlist into a single, print-ready PDF."""
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

log = logging.getLogger("noder")

MONTHS = ["januar", "februar", "marts", "april", "maj", "juni", "juli",
          "august", "september", "oktober", "november", "december"]

INK = HexColor("#1f2433")
MUTED = HexColor("#6b7280")
ACCENT = HexColor("#4f46e5")
RULE = HexColor("#d9dce3")


def danish_date(d: date) -> str:
    return f"{d.day}. {MONTHS[d.month - 1]} {d.year}"


def safe_filename(name: str) -> str:
    """'Julekoncert 2026!' -> 'Julekoncert-2026'"""
    name = unicodedata.normalize("NFKD", name.replace("æ", "ae").replace("Æ", "Ae")
                                 .replace("ø", "oe").replace("Ø", "Oe").replace("å", "aa").replace("Å", "Aa"))
    name = name.encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:80] or "setliste"


@dataclass
class ExportEntry:
    kind: str  # "song" | "separator"
    title: str
    composer: str | None = None
    pdf_paths: list[str] = field(default_factory=list)
    # filled in while building
    start_page: int | None = None
    page_count: int = 0


@dataclass
class ExportOptions:
    cover: bool = True
    duplex: bool = False        # every song starts on a right-hand (odd) page
    page_numbers: bool = False  # stamp page numbers in the bottom corner


def pick_pdfs(files, mode: str, part: str = "") -> list:
    """Choose which of a song's PDF files to include."""
    pdfs = sorted((f for f in files if f.file_type == "pdf"), key=lambda f: f.id)
    if mode == "all" or not pdfs:
        return pdfs
    if mode == "part" and part:
        wanted = part.strip().lower()
        names = [(f, (f.part_name or f.original_filename or "").strip().lower()) for f in pdfs]
        exact = [f for f, n in names if n == wanted]
        partial = [f for f, n in names if wanted in n]
        if exact or partial:
            return (exact or partial)[:1]
    return pdfs[:1]


# ---------------------------------------------------------------------------
# Cover page
# ---------------------------------------------------------------------------
def _fit(text: str, font: str, size: float, max_width: float) -> str:
    if stringWidth(text, font, size) <= max_width:
        return text
    while text and stringWidth(text + "…", font, size) > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(text: str, font: str, size: float, max_width: float) -> list[str]:
    lines = []
    for paragraph in text.splitlines():
        words, line = paragraph.split(), ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if stringWidth(candidate, font, size) <= max_width:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = _fit(word, font, size, max_width)
        lines.append(line)
    return lines


def _render_cover(title: str, choir: str | None, event_date: date | None, notes: str | None,
                  entries: list[ExportEntry]) -> bytes:
    buf = io.BytesIO()
    w, h = A4
    margin = 64
    right = w - margin
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(title)

    def footer():
        c.setFont("Helvetica", 8)
        c.setFillColor(MUTED)
        c.drawString(margin, 36, f"Genereret med Noder · {danish_date(date.today())}")

    # Header block
    y = h - 92
    c.setFillColor(ACCENT)
    c.rect(margin, y + 30, 36, 4, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(margin, y + 12, "SETLISTE")
    c.setFillColor(INK)
    for line in _wrap(title, "Helvetica-Bold", 28, right - margin)[:3]:
        y -= 30
        c.setFont("Helvetica-Bold", 28)
        c.drawString(margin, y, line)

    meta = " · ".join(x for x in [choir, danish_date(event_date) if event_date else None] if x)
    if meta:
        y -= 22
        c.setFont("Helvetica", 12)
        c.setFillColor(MUTED)
        c.drawString(margin, y, meta)

    if notes:
        y -= 10
        c.setFont("Helvetica-Oblique", 10)
        c.setFillColor(INK)
        for line in _wrap(notes, "Helvetica-Oblique", 10, right - margin)[:8]:
            y -= 14
            c.drawString(margin, y, line)

    y -= 34
    c.setStrokeColor(RULE)
    c.setLineWidth(0.8)
    c.line(margin, y + 14, right, y + 14)

    # Table of contents
    number = 0
    for entry in entries:
        row_h = 30 if entry.kind == "separator" else (28 if entry.composer else 20)
        if y - row_h < 70:
            footer()
            c.showPage()
            y = h - 72
        if entry.kind == "separator":
            y -= 22
            c.setFont("Helvetica-Bold", 9)
            c.setFillColor(ACCENT)
            c.drawString(margin, y, _fit(entry.title.upper(), "Helvetica-Bold", 9, right - margin))
            y -= 8
            continue

        number += 1
        y -= 18
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(MUTED)
        c.drawRightString(margin + 18, y, f"{number}")

        page_label = str(entry.start_page) if entry.start_page else "—"
        num_w = stringWidth(page_label, "Helvetica", 11)
        title_x = margin + 30
        title_text = _fit(entry.title, "Helvetica", 12, right - title_x - num_w - 40)
        c.setFont("Helvetica", 12)
        c.setFillColor(INK if entry.start_page else MUTED)
        c.drawString(title_x, y, title_text)

        # dotted leader
        lead_from = title_x + stringWidth(title_text, "Helvetica", 12) + 6
        lead_to = right - num_w - 6
        if lead_to > lead_from:
            c.setStrokeColor(RULE)
            c.setDash(1, 3)
            c.line(lead_from, y + 1, lead_to, y + 1)
            c.setDash()
        c.setFont("Helvetica", 11)
        c.drawRightString(right, y, page_label)

        sub = entry.composer or ""
        if not entry.start_page:
            sub = (sub + " · " if sub else "") + "ingen noder"
        if sub:
            y -= 12
            c.setFont("Helvetica", 9)
            c.setFillColor(MUTED)
            c.drawString(title_x, y, _fit(sub, "Helvetica", 9, right - title_x - 40))

    footer()
    c.save()
    return buf.getvalue()


def _render_page_numbers(sizes: list[tuple[float, float]], first_number: int) -> PdfReader:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i, (w, h) in enumerate(sizes):
        c.setPageSize((w, h))
        c.setFont("Helvetica", 8)
        c.setFillColor(MUTED)
        c.drawRightString(w - 22, 16, str(first_number + i))
        c.showPage()
    c.save()
    buf.seek(0)
    return PdfReader(buf)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build_setlist_pdf(title: str, choir: str | None, event_date: date | None, notes: str | None,
                      entries: list[ExportEntry], options: ExportOptions) -> tuple[bytes, list[str]]:
    """Return (pdf bytes, warnings). Unreadable PDFs are skipped with a warning."""
    warnings: list[str] = []
    readers: dict[int, list[PdfReader]] = {}
    for idx, entry in enumerate(entries):
        if entry.kind != "song":
            continue
        for path in entry.pdf_paths:
            try:
                reader = PdfReader(path)
                if reader.is_encrypted:
                    reader.decrypt("")
                len(reader.pages)
            except (PyPdfError, OSError, ValueError, NotImplementedError) as e:
                log.warning("Springer PDF over (%s): %s", path, e)
                warnings.append(entry.title)
                continue
            readers.setdefault(idx, []).append(reader)

    def layout(cover_pages: int):
        page = cover_pages + 1
        for idx, entry in enumerate(entries):
            entry.start_page, entry.page_count = None, 0
            if idx not in readers:
                continue
            if options.duplex and page % 2 == 0:
                page += 1
            entry.start_page = page
            entry.page_count = sum(len(r.pages) for r in readers[idx])
            page += entry.page_count

    cover_bytes = b""
    if options.cover:
        # Two passes: the cover's own length decides where the songs start
        layout(0)
        cover_bytes = _render_cover(title, choir, event_date, notes, entries)
        cover_pages = len(PdfReader(io.BytesIO(cover_bytes)).pages)
        if options.duplex and cover_pages % 2:
            cover_pages += 1
        layout(cover_pages)
        cover_bytes = _render_cover(title, choir, event_date, notes, entries)
    else:
        layout(0)

    writer = PdfWriter()
    if cover_bytes:
        writer.append(PdfReader(io.BytesIO(cover_bytes)), import_outline=False)
        if options.duplex and len(writer.pages) % 2:
            writer.add_blank_page(*A4)
    content_start = len(writer.pages)

    parent = None
    for idx, entry in enumerate(entries):
        if entry.kind == "separator":
            parent = None
            nxt = next((e for e in entries[idx + 1:] if e.start_page), None)
            if nxt:
                parent = writer.add_outline_item(entry.title, nxt.start_page - 1)
            continue
        if idx not in readers:
            continue
        while len(writer.pages) < entry.start_page - 1:  # duplex padding
            last = writer.pages[-1].mediabox
            writer.add_blank_page(float(last.width), float(last.height))
        for reader in readers[idx]:
            writer.append(reader, import_outline=False)
        writer.add_outline_item(entry.title, entry.start_page - 1, parent=parent)

    if options.page_numbers and len(writer.pages) > content_start:
        body = writer.pages[content_start:]
        sizes = [(float(p.mediabox.width), float(p.mediabox.height)) for p in body]
        overlay = _render_page_numbers(sizes, content_start + 1)
        for page, stamp in zip(body, overlay.pages):
            page.merge_page(stamp)

    if not writer.pages:
        writer.add_blank_page(*A4)
    writer.add_metadata({"/Title": title, "/Creator": "Noder"})
    writer.page_mode = "/UseOutlines"

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), warnings
