"""Page geometry: turn script rows into pixel rectangles for each panel."""

from __future__ import annotations

from weaver.story import Page

Rect = tuple[int, int, int, int]


def page_cells(page: Page, cfg: dict) -> dict[str, Rect]:
    width, height = int(cfg["width"]), int(cfg["height"])
    margin, gutter = int(cfg["margin"]), int(cfg["gutter"])
    inner_w = width - 2 * margin
    inner_h = height - 2 * margin - gutter * (len(page.rows) - 1)
    total_h = sum(r.height for r in page.rows)
    cells: dict[str, Rect] = {}
    y = float(margin)
    for row in page.rows:
        row_h = inner_h * row.height / total_h
        avail_w = inner_w - gutter * (len(row.panels) - 1)
        total_w = sum(row.widths)
        x = float(margin)
        for pid, w in zip(row.panels, row.widths):
            cell_w = avail_w * w / total_w
            cells[pid] = (round(x), round(y), round(x + cell_w), round(y + row_h))
            x += cell_w + gutter
        y += row_h + gutter
    return cells


def cell_aspect(rect: Rect) -> float:
    x0, y0, x1, y1 = rect
    return (x1 - x0) / (y1 - y0)
