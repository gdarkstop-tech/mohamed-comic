"""English comic lettering: speech and thought balloons, captions, sound effects.

Lettering is drawn by code, never by the image model, so text is always
spelled correctly and can be edited without regenerating artwork.
Shapes are built as masks and outlined by dilation, so tails join balloons
seamlessly. Everything is clipped to its panel.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from weaver.config import FONTS_DIR
from weaver.story import Lettering

DIALOGUE_FONT = FONTS_DIR / "comicneue-ComicNeue-Bold.ttf"
SFX_FONT = FONTS_DIR / "bangers-Bangers-Regular.ttf"
CAPTION_FILL = (250, 226, 110)
INK = (12, 12, 12)
PAPER = (255, 255, 255)

Rect = tuple[int, int, int, int]


def _font(path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) <= max_width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def block_size(draw: ImageDraw.ImageDraw, lines: list[str], font, spacing: int) -> tuple[int, int, int]:
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    width = max(int(math.ceil(draw.textlength(line, font=font))) for line in lines)
    return width, line_h * len(lines) + spacing * (len(lines) - 1), line_h


def draw_lines(draw, lines, font, origin, line_h, spacing, block_w, align="center", fill=INK) -> None:
    x0, y0 = origin
    for i, line in enumerate(lines):
        lw = draw.textlength(line, font=font)
        x = x0 + (block_w - lw) / 2 if align == "center" else x0
        draw.text((x, y0 + i * (line_h + spacing)), line, font=font, fill=fill)


def anchor_point(at: str, cell_w: int, cell_h: int, box_w: int, box_h: int, inset: int) -> tuple[int, int]:
    vertical, _, horizontal = at.partition("-") if "-" in at else (at, "", "")
    if at in ("left", "right"):
        vertical, horizontal = "middle", at
    if at in ("top", "middle", "bottom"):
        vertical, horizontal = at, "center"
    x = {"left": inset, "center": (cell_w - box_w) // 2, "right": cell_w - box_w - inset}.get(horizontal or "center")
    y = {"top": inset, "middle": (cell_h - box_h) // 2, "bottom": cell_h - box_h - inset}[vertical]
    return int(x), int(y)


class Letterer:
    def __init__(self, page: Image.Image, scale: float = 1.0):
        self.page = page
        self.scale = scale
        self.stroke = max(3, round(4 * scale))
        self.dialogue = _font(DIALOGUE_FONT, round(40 * scale))
        self.caption = _font(DIALOGUE_FONT, round(34 * scale))

    def letter_panel(self, rect: Rect, items: tuple[Lettering, ...] | list[Lettering]) -> None:
        x0, y0, x1, y1 = rect
        cell_w, cell_h = x1 - x0, y1 - y0
        layer = Image.new("RGBA", (cell_w, cell_h), (0, 0, 0, 0))
        placed: list[tuple[int, int, int, int]] = []
        gap = round(14 * self.scale)
        for item in items:
            if item.type == "sfx":
                self._sfx(layer, item)
                continue
            box, painter = self._prepare(item, cell_w, cell_h)
            bw, bh = box
            inset = round((14 if item.type == "caption" else 26) * self.scale)
            ax, ay = anchor_point(item.at, cell_w, cell_h, bw, bh, inset)
            upward = item.at.startswith("bottom")
            # Slide past earlier lettering in reading direction instead of covering it.
            moved = True
            while moved:
                moved = False
                for (px0, py0, px1, py1) in placed:
                    if ax < px1 + gap and ax + bw + gap > px0 and ay < py1 + gap and ay + bh + gap > py0:
                        ay = py0 - bh - gap if upward else py1 + gap
                        moved = True
            ay = max(0, min(cell_h - bh, ay))
            placed.append((ax, ay, ax + bw, ay + bh))
            painter(layer, ax, ay)
        self.page.paste(layer, (x0, y0), layer)

    # ------------------------------------------------------------------ shapes
    def _prepare(self, item: Lettering, cell_w: int, cell_h: int):
        draw = ImageDraw.Draw(self.page)
        font = self.caption if item.type == "caption" else self.dialogue
        text = item.text.upper()
        max_w = int(min(cell_w * (0.62 if item.type == "caption" else 0.5), 640 * self.scale))
        lines = wrap(draw, text, font, max(120, max_w))
        spacing = round(4 * self.scale)
        tw, th, line_h = block_size(draw, lines, font, spacing)
        if item.type == "caption":
            pad = round(16 * self.scale)
            bw, bh = tw + 2 * pad, th + 2 * pad

            def paint(layer, ax, ay):
                d = ImageDraw.Draw(layer)
                d.rectangle((ax, ay, ax + bw, ay + bh), fill=CAPTION_FILL + (255,), outline=INK + (255,),
                            width=self.stroke)
                draw_lines(d, lines, font, (ax + pad, ay + pad), line_h, spacing, tw, align="left")
            return (bw, bh), paint

        pad = round(18 * self.scale)
        rx = tw / 2 * 1.32 + pad
        ry = th / 2 * 1.42 + pad
        bw, bh = int(2 * rx), int(2 * ry)

        def paint(layer, ax, ay):
            cx, cy = ax + rx, ay + ry
            mask = Image.new("L", layer.size, 0)
            md = ImageDraw.Draw(mask)
            if item.type == "thought":
                self._cloud(md, cx, cy, rx, ry)
                if item.tail:
                    self._thought_tail(md, cx, cy, rx, ry, item.tail, layer.size)
            else:
                md.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=255)
                if item.tail:
                    md.polygon(self._tail(cx, cy, rx, ry, item.tail, layer.size), fill=255)
            outline = mask.filter(ImageFilter.MaxFilter(2 * self.stroke + 1))
            layer.paste(Image.new("RGBA", layer.size, INK + (255,)), (0, 0), outline)
            layer.paste(Image.new("RGBA", layer.size, PAPER + (255,)), (0, 0), mask)
            d = ImageDraw.Draw(layer)
            draw_lines(d, lines, font, (cx - tw / 2, cy - th / 2), line_h, spacing, tw)
        return (bw, bh), paint

    def _tail(self, cx, cy, rx, ry, tail, size):
        tx, ty = tail[0] * size[0], tail[1] * size[1]
        angle = math.atan2(ty - cy, tx - cx)
        dist = math.hypot(tx - cx, ty - cy)
        edge = math.hypot(rx * math.cos(angle), ry * math.sin(angle))
        length = min(max(dist, edge + 30 * self.scale), edge + 170 * self.scale)
        tip = (cx + length * math.cos(angle), cy + length * math.sin(angle))
        spread = 0.2
        base = [(cx + rx * 0.8 * math.cos(angle + s), cy + ry * 0.8 * math.sin(angle + s)) for s in (-spread, spread)]
        return [base[0], tip, base[1]]

    def _cloud(self, md, cx, cy, rx, ry):
        md.ellipse((cx - rx * 0.92, cy - ry * 0.88, cx + rx * 0.92, cy + ry * 0.88), fill=255)
        bumps = max(9, int((rx + ry) / (28 * self.scale)))
        for i in range(bumps):
            a = 2 * math.pi * i / bumps
            bx, by = cx + rx * 0.82 * math.cos(a), cy + ry * 0.78 * math.sin(a)
            r = min(rx, ry) * 0.36
            md.ellipse((bx - r, by - r, bx + r, by + r), fill=255)

    def _thought_tail(self, md, cx, cy, rx, ry, tail, size):
        tx, ty = tail[0] * size[0], tail[1] * size[1]
        angle = math.atan2(ty - cy, tx - cx)
        edge = math.hypot(rx * math.cos(angle), ry * math.sin(angle))
        for i, frac in enumerate((0.25, 0.55, 0.85)):
            d = edge + frac * 120 * self.scale
            r = (16 - i * 4.5) * self.scale
            px, py = cx + d * math.cos(angle), cy + d * math.sin(angle)
            md.ellipse((px - r, py - r, px + r, py + r), fill=255)

    def _sfx(self, layer: Image.Image, item: Lettering) -> None:
        size = max(round(70 * self.scale), int(layer.height * 0.16))
        font = _font(SFX_FONT, size)
        text = item.text.upper()
        probe = ImageDraw.Draw(layer)
        stroke = max(4, size // 10)
        left, top, right, bottom = probe.textbbox((0, 0), text, font=font, stroke_width=stroke)
        tile = Image.new("RGBA", (right - left + 20, bottom - top + 20), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((10 - left, 10 - top), text, font=font, fill=(255, 196, 22, 255),
                                  stroke_width=stroke, stroke_fill=INK + (255,))
        tile = tile.rotate(-8 if "right" in item.at else 8, resample=Image.BICUBIC, expand=True)
        inset = round(20 * self.scale)
        ax, ay = anchor_point(item.at, layer.width, layer.height, tile.width, tile.height, inset)
        layer.alpha_composite(tile, (max(0, ax), max(0, ay)))
