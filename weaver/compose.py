"""Compose pages from selected panel artwork, letter them, and export a PDF."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from weaver.balloons import DIALOGUE_FONT, INK, Letterer
from weaver.imaging import cover_fit, load_rgb
from weaver.layout import page_cells
from weaver.state import ProductionState
from weaver.story import Script


class MissingArtwork(Exception):
    pass


def compose_page(script: Script, number: int, state: ProductionState, page_cfg: dict, out_dir: Path) -> Path:
    page = script.page(number)
    cells = page_cells(page, page_cfg)
    missing = [p.id for p in page.panels if state.selected_version(p.id) is None]
    if missing:
        raise MissingArtwork(f"Page {number} cannot be composed; no artwork yet for: {', '.join(missing)}")

    width, height = int(page_cfg["width"]), int(page_cfg["height"])
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    letterer = Letterer(canvas, scale=width / 1988)
    border = int(page_cfg["border"])

    for panel in page.panels:
        state.verify_locked(panel.id)
        version = state.selected_version(panel.id)
        art = load_rgb(state.workspace / version["file"])
        x0, y0, x1, y1 = cells[panel.id]
        canvas.paste(cover_fit(art, x1 - x0, y1 - y0, panel.focus), (x0, y0))
        letterer.letter_panel(cells[panel.id], panel.lettering)
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=INK, width=border)

    folio = ImageFont.truetype(str(DIALOGUE_FONT), max(18, width // 80))
    label = str(number)
    draw.text(((width - draw.textlength(label, font=folio)) / 2, height - int(page_cfg["margin"]) * 0.7),
              label, font=folio, fill=INK)

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"page-{number:02d}.png"
    canvas.save(out, dpi=(int(page_cfg["dpi"]),) * 2)
    return out


def export_pdf(page_files: list[Path], out: Path, dpi: int) -> Path:
    if not page_files:
        raise MissingArtwork("No composed pages to export.")
    images = [load_rgb(p) for p in page_files]
    out.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(out, format="PDF", save_all=True, append_images=images[1:], resolution=float(dpi))
    return out
