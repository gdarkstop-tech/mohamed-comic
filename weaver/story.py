"""Load and validate the story: character bible, house style and chapter scripts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from weaver.config import STORY_DIR

LETTERING_TYPES = {"caption", "speech", "thought", "sfx"}
PLACEMENTS = {
    "top-left", "top", "top-right", "middle-left", "middle", "middle-right",
    "bottom-left", "bottom", "bottom-right",
}


class StoryError(Exception):
    pass


@dataclass(frozen=True)
class Character:
    id: str
    name: str
    look: str
    outfit: str
    refs: tuple[str, ...]
    role: str = ""


@dataclass(frozen=True)
class Lettering:
    type: str
    text: str
    at: str = "top-left"
    speaker: str | None = None
    tail: tuple[float, float] | None = None


@dataclass(frozen=True)
class Panel:
    id: str
    page: int
    shot: str
    scene: str
    characters: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    seed: int | None = None
    setting: bool = False
    focus: tuple[float, float] = (0.5, 0.5)
    lettering: tuple[Lettering, ...] = ()


@dataclass(frozen=True)
class Row:
    height: float
    panels: tuple[str, ...]
    widths: tuple[float, ...]


@dataclass(frozen=True)
class Page:
    number: int
    rows: tuple[Row, ...]
    panels: tuple[Panel, ...]


@dataclass(frozen=True)
class Script:
    chapter: int
    title: str
    status: str
    pilot_pages: tuple[int, ...]
    pages: tuple[Page, ...]
    path: Path = field(compare=False, default=Path())

    def page(self, number: int) -> Page:
        for p in self.pages:
            if p.number == number:
                return p
        raise StoryError(f"Page {number} is not in the script")

    def panel(self, panel_id: str) -> Panel:
        for p in self.pages:
            for panel in p.panels:
                if panel.id == panel_id:
                    return panel
        raise StoryError(f"Panel {panel_id} is not in the script")

    def all_panels(self) -> list[Panel]:
        return [panel for p in self.pages for panel in p.panels]


@dataclass(frozen=True)
class Style:
    description: str
    no_text: str
    identity_rule: str
    style_ref: str | None
    setting_ref: str | None


def load_characters(path: Path = STORY_DIR / "characters.yaml") -> dict[str, Character]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["characters"]
    return {
        cid: Character(id=cid, name=d["name"], look=d.get("look", "").strip(), outfit=d.get("outfit", "").strip(),
                       refs=tuple(d.get("refs", [])), role=d.get("role", ""))
        for cid, d in raw.items()
    }


def load_style(path: Path = STORY_DIR / "style.yaml") -> Style:
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Style(description=d["description"].strip(), no_text=d["no_text"].strip(),
                 identity_rule=d["identity_rule"].strip(), style_ref=d.get("style_ref"),
                 setting_ref=d.get("setting_ref"))


def default_script_path() -> Path:
    return STORY_DIR / "chapter-01" / "script.yaml"


def load_script(path: Path | None = None, characters: dict[str, Character] | None = None,
                ref_ids: set[str] | None = None) -> Script:
    path = path or default_script_path()
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    pages = []
    seen: set[str] = set()
    for pd in d["pages"]:
        number = int(pd["number"])
        panels = []
        for p in pd["panels"]:
            pid = p["id"]
            if pid in seen:
                raise StoryError(f"Duplicate panel id {pid}")
            seen.add(pid)
            lettering = []
            for item in p.get("lettering", []):
                lt = Lettering(type=item["type"], text=str(item["text"]), at=item.get("at", "top-left"),
                               speaker=item.get("speaker"), tail=tuple(item["tail"]) if item.get("tail") else None)
                if lt.type not in LETTERING_TYPES:
                    raise StoryError(f"{pid}: unknown lettering type '{lt.type}'")
                if lt.at not in PLACEMENTS:
                    raise StoryError(f"{pid}: unknown placement '{lt.at}'")
                if characters is not None and lt.speaker and lt.speaker not in characters:
                    raise StoryError(f"{pid}: unknown speaker '{lt.speaker}'")
                lettering.append(lt)
            panel = Panel(
                id=pid, page=number, shot=p.get("shot", "").strip(), scene=" ".join(p["scene"].split()),
                characters=tuple(p.get("characters", [])), refs=tuple(p.get("refs", [])),
                seed=p.get("seed"), setting=bool(p.get("setting", False)),
                focus=tuple(p.get("focus", (0.5, 0.5))), lettering=tuple(lettering),
            )
            if characters is not None:
                for c in panel.characters:
                    if c not in characters:
                        raise StoryError(f"{pid}: unknown character '{c}'")
            if ref_ids is not None:
                for r in panel.refs:
                    if r not in ref_ids:
                        raise StoryError(f"{pid}: unknown reference '{r}'")
            panels.append(panel)
        rows = []
        in_layout: list[str] = []
        for rd in pd["rows"]:
            ids = tuple(rd["panels"])
            widths = tuple(float(w) for w in rd.get("widths", [1] * len(ids)))
            if len(widths) != len(ids):
                raise StoryError(f"Page {number}: widths do not match panels in a row")
            rows.append(Row(height=float(rd.get("height", 1)), panels=ids, widths=widths))
            in_layout.extend(ids)
        defined = [p.id for p in panels]
        if sorted(in_layout) != sorted(defined):
            raise StoryError(f"Page {number}: layout panels {in_layout} do not match defined panels {defined}")
        pages.append(Page(number=number, rows=tuple(rows), panels=tuple(panels)))
    return Script(chapter=int(d["chapter"]), title=d["title"], status=str(d.get("status", "draft")),
                  pilot_pages=tuple(d.get("pilot_pages", [])), pages=tuple(pages), path=path)
