"""Image helpers: reference preparation, output verification, sizing."""

from __future__ import annotations

import io
import math

from PIL import Image, ImageOps, ImageStat


def load_rgb(path) -> Image.Image:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return im.convert("RGB")


def fit_within(im: Image.Image, max_side: int) -> Image.Image:
    """Downscale (never upscale) so both sides are <= max_side, keeping aspect."""
    scale = min(1.0, max_side / im.width, max_side / im.height)
    if scale >= 1.0:
        return im.copy()
    size = (max(1, math.floor(im.width * scale)), max(1, math.floor(im.height * scale)))
    return im.resize(size, Image.LANCZOS)


def to_png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class InvalidImage(Exception):
    pass


def verify_generated(data: bytes, min_side: int = 64, min_stddev: float = 4.0) -> tuple[int, int, str]:
    """Prove the bytes are a real, decodable, non-blank image. Returns (w, h, format)."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            fmt = (im.format or "").lower()
            width, height = im.size
            stats = ImageStat.Stat(im.convert("RGB"))
    except Exception as exc:  # Pillow raises many types for corrupt data
        raise InvalidImage(f"output is not a decodable image: {exc}") from exc
    if width < min_side or height < min_side:
        raise InvalidImage(f"output is only {width}x{height}")
    if max(stats.stddev) < min_stddev:
        raise InvalidImage("output is a blank / single-colour image")
    return width, height, fmt


def generation_size(aspect: float, megapixels: float, min_side: int, max_side: int, multiple: int) -> tuple[int, int]:
    """Pick an output size with the given aspect (w/h) near the target megapixels."""
    area = megapixels * 1_000_000
    width = math.sqrt(area * aspect)
    height = width / aspect

    def snap(v: float) -> int:
        return int(max(min_side, min(max_side, round(v / multiple) * multiple)))

    w, h = snap(width), snap(height)
    # Re-balance if clamping one side distorted the aspect too much.
    if w == max_side or w == min_side:
        h = snap(w / aspect)
    elif h == max_side or h == min_side:
        w = snap(h * aspect)
    return w, h


def cover_fit(im: Image.Image, width: int, height: int, focus: tuple[float, float] = (0.5, 0.5)) -> Image.Image:
    """Scale to cover (width, height) and crop around the focus point (relative coords)."""
    scale = max(width / im.width, height / im.height)
    resized = im.resize((math.ceil(im.width * scale), math.ceil(im.height * scale)), Image.LANCZOS)
    fx, fy = focus
    left = int(round((resized.width - width) * min(1, max(0, fx))))
    top = int(round((resized.height - height) * min(1, max(0, fy))))
    return resized.crop((left, top, left + width, top + height))
