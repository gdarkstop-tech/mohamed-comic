"""Reference image registry: crop, resize to provider limits, hash, preview."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from PIL import Image, ImageDraw

from weaver.config import REFERENCES_DIR, STORY_DIR
from weaver.imaging import fit_within, load_rgb, to_png_bytes
from weaver.providers.base import ReferenceInput
from weaver.state import sha256_bytes

SPEC_FILE = STORY_DIR / "references.yaml"
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".tif", ".tiff"}


@dataclass(frozen=True)
class RefSpec:
    id: str
    source: str
    kind: str
    role: str
    box: tuple[int, int, int, int] | None = None
    box_rel: tuple[float, float, float, float] | None = None
    masks: tuple[tuple[int, int, int, int], ...] = field(default_factory=tuple)


def load_specs(path: Path = SPEC_FILE) -> dict[str, RefSpec]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["references"]
    specs = {}
    for ref_id, d in raw.items():
        specs[ref_id] = RefSpec(
            id=ref_id, source=d["source"], kind=d.get("kind", "art"), role=d.get("role", "identity"),
            box=tuple(d["box"]) if d.get("box") else None,
            box_rel=tuple(d["box_rel"]) if d.get("box_rel") else None,
            masks=tuple(tuple(m) for m in d.get("masks", [])),
        )
    return specs


class ReferenceLibrary:
    def __init__(self, root: Path = REFERENCES_DIR, specs: dict[str, RefSpec] | None = None):
        self.root = root
        self.specs = specs if specs is not None else load_specs()

    def source_path(self, ref_id: str) -> Path | None:
        spec = self.specs[ref_id]
        if "*" in spec.source:
            folder, pattern = (self.root / spec.source).parent, Path(spec.source).name
            matches = sorted(p for p in folder.glob(pattern) if p.suffix.lower() in PHOTO_EXTS)
            return matches[0] if matches else None
        path = self.root / spec.source
        return path if path.exists() else None

    def available(self, ref_id: str) -> bool:
        return ref_id in self.specs and self.source_path(ref_id) is not None

    def crop(self, ref_id: str) -> Image.Image:
        spec = self.specs[ref_id]
        path = self.source_path(ref_id)
        if path is None:
            raise FileNotFoundError(f"Reference '{ref_id}' needs {self.root / spec.source} (not found).")
        im = load_rgb(path)
        if spec.masks:
            draw = ImageDraw.Draw(im)
            for m in spec.masks:
                draw.rectangle(m, fill=(255, 255, 255))
        if spec.box:
            im = im.crop(spec.box)
        elif spec.box_rel:
            l, t, r, b = spec.box_rel
            im = im.crop((round(l * im.width), round(t * im.height), round(r * im.width), round(b * im.height)))
        return im

    def prepare(self, ref_id: str, max_side: int) -> ReferenceInput:
        """Crop + downscale to fit the provider limit; returns PNG bytes and their hash."""
        im = fit_within(self.crop(ref_id), max_side)
        data = to_png_bytes(im)
        return ReferenceInput(ref_id=ref_id, png_bytes=data, width=im.width, height=im.height, sha256=sha256_bytes(data))

    def import_photo(self, src: Path, character: str) -> Path:
        if src.suffix.lower() not in PHOTO_EXTS:
            raise ValueError(f"{src} is not a supported image type")
        load_rgb(src)  # proves it decodes before we accept it
        dest_dir = self.root / "photos"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for old in dest_dir.glob(f"{character}.*"):
            old.unlink()
        dest = dest_dir / f"{character}{src.suffix.lower()}"
        shutil.copy2(src, dest)
        return dest

    def contact_sheet(self, ref_ids: list[str], out: Path, max_side: int = 512, thumb_h: int = 260) -> Path:
        tiles = []
        for ref_id in ref_ids:
            if not self.available(ref_id):
                continue
            im = fit_within(self.crop(ref_id), max_side)
            im = im.resize((max(1, round(im.width * thumb_h / im.height)), thumb_h))
            tile = Image.new("RGB", (max(im.width, 160), thumb_h + 28), "white")
            tile.paste(im, (0, 0))
            ImageDraw.Draw(tile).text((4, thumb_h + 6), ref_id, fill="black")
            tiles.append(tile)
        if not tiles:
            raise FileNotFoundError("No reference images are available to preview.")
        per_row, gap = 6, 10
        rows = [tiles[i:i + per_row] for i in range(0, len(tiles), per_row)]
        width = max(sum(t.width for t in r) + gap * (len(r) - 1) for r in rows)
        height = sum(max(t.height for t in r) for r in rows) + gap * (len(rows) - 1)
        sheet = Image.new("RGB", (width, height), (235, 235, 235))
        y = 0
        for r in rows:
            x = 0
            for t in r:
                sheet.paste(t, (x, y))
                x += t.width + gap
            y += max(t.height for t in r) + gap
        out.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out)
        return out
