"""Choose reference images for a panel and write the generation prompt.

Reference images are numbered exactly as they are sent (input_image_0 is
"image 0"), which is how FLUX.2 prompts refer to them.
"""

from __future__ import annotations

from dataclasses import dataclass

from weaver.refs import ReferenceLibrary
from weaver.story import Character, Panel, Style


@dataclass(frozen=True)
class RefSlot:
    ref_id: str
    purpose: str            # identity | drawn | outfit | style | setting | explicit
    character: str | None = None
    kind: str = "art"       # photo | art
    style_anchor: bool = False  # art in the house style: "draw it in the style of this image"


@dataclass(frozen=True)
class PanelPlan:
    prompt: str
    slots: tuple[RefSlot, ...]
    warnings: tuple[str, ...]


def _first_available(lib: ReferenceLibrary, ids, roles: set[str] | None = None, skip=(),
                     kind: str | None = None) -> str | None:
    for ref_id in ids:
        if ref_id in skip or ref_id not in lib.specs:
            continue
        if roles and lib.specs[ref_id].role not in roles:
            continue
        if kind and lib.specs[ref_id].kind != kind:
            continue
        if lib.available(ref_id):
            return ref_id
    return None


def select_refs(panel: Panel, bible: dict[str, Character], style: Style, lib: ReferenceLibrary,
                max_refs: int) -> tuple[list[RefSlot], list[str]]:
    warnings: list[str] = []
    if panel.refs:
        slots = []
        for ref_id in panel.refs:
            if lib.available(ref_id):
                slots.append(RefSlot(ref_id, "explicit"))
            else:
                warnings.append(f"reference '{ref_id}' is not on disk; skipped")
        return slots[:max_refs], warnings

    slots: list[RefSlot] = []
    used: set[str] = set()
    for cid in panel.characters:
        char = bible[cid]
        ref = _first_available(lib, char.refs, {"identity", "costume"})
        if ref is None:
            warnings.append(f"no reference image available for {char.name}; relying on text only")
            continue
        if lib.specs[ref].kind != "photo" and any(lib.specs.get(r) and lib.specs[r].kind == "photo" for r in char.refs):
            warnings.append(f"{char.name}: real photo not on disk, using approved artwork for identity")
        if len(slots) < max_refs:
            spec = lib.specs[ref]
            slots.append(RefSlot(ref, "identity", cid, spec.kind,
                                 style_anchor=spec.kind == "art" and spec.source in style.house_sources))
            used.add(ref)
    # The character as already drawn in the approved artwork: carries both the face
    # in the house style and the style itself, so it outranks a generic style anchor.
    for cid in panel.characters:
        if len(slots) >= max_refs:
            break
        ref = _first_available(lib, bible[cid].refs, {"identity", "costume"}, skip=used, kind="art")
        if ref:
            slots.append(RefSlot(ref, "drawn", cid))
            used.add(ref)
    has_anchor = any(s.purpose == "drawn" or s.style_anchor for s in slots)
    if not has_anchor and style.style_ref and lib.available(style.style_ref) and len(slots) < max_refs:
        slots.append(RefSlot(style.style_ref, "style"))
        used.add(style.style_ref)
    if (panel.setting and style.setting_ref and style.setting_ref not in used
            and lib.available(style.setting_ref) and len(slots) < max_refs):
        slots.append(RefSlot(style.setting_ref, "setting"))
        used.add(style.setting_ref)
    for cid in panel.characters:
        if len(slots) >= max_refs:
            break
        ref = _first_available(lib, bible[cid].refs, {"outfit"}, skip=used)
        if ref:
            slots.append(RefSlot(ref, "outfit", cid))
            used.add(ref)
    return slots, warnings


def build_prompt(panel: Panel, bible: dict[str, Character], style: Style, slots: list[RefSlot]) -> str:
    index = {(s.purpose, s.character): i for i, s in enumerate(slots)}
    anchors = [i for i, s in enumerate(slots) if s.purpose == "drawn" or s.style_anchor]
    opening = style.medium or "Comic book panel"
    lines = [f"{opening}, {panel.shot}: {panel.scene}." if panel.shot else f"{opening}: {panel.scene}."]
    has_identity = False
    for cid in panel.characters:
        char = bible[cid]
        text = f"{char.name}: {char.look}"
        if char.outfit:
            text += f"; wearing {char.outfit}"
        refs = []
        if ("identity", cid) in index:
            i = index[("identity", cid)]
            if slots[i].kind == "photo":
                refs.append(f"image {i} is a real photo of {char.name}: keep this exact face, face shape and skin tone")
            else:
                refs.append(f"image {i} shows {char.name}'s exact appearance")
            has_identity = True
        if ("drawn", cid) in index:
            refs.append(f"image {index[('drawn', cid)]} shows {char.name} as drawn in this comic")
        if ("outfit", cid) in index:
            refs.append(f"image {index[('outfit', cid)]} shows {char.name}'s outfit")
        if refs:
            text += " (" + "; ".join(refs) + ")"
        lines.append(text + ".")
    explicit = [i for i, s in enumerate(slots) if s.purpose == "explicit"]
    if explicit:
        lines.append("Use " + ", ".join(f"image {i}" for i in explicit) + " as visual references for this scene.")
    if has_identity:
        lines.append(style.identity_rule)
    if anchors:
        lines.append(f"Draw the whole image in exactly the same art style as "
                     f"{', '.join(f'image {i}' for i in anchors)}: {style.description}.")
    elif ("style", None) in index:
        lines.append(f"Draw it in the exact art style of image {index[('style', None)]}: {style.description}.")
    else:
        lines.append(f"Art style: {style.description}.")
    if ("setting", None) in index:
        lines.append(f"Use image {index[('setting', None)]} as the reference for the Cairo skyline.")
    lines.append(style.no_text)
    return " ".join(lines)


def plan_panel(panel: Panel, bible: dict[str, Character], style: Style, lib: ReferenceLibrary,
               max_refs: int) -> PanelPlan:
    slots, warnings = select_refs(panel, bible, style, lib, max_refs)
    return PanelPlan(prompt=build_prompt(panel, bible, style, slots), slots=tuple(slots), warnings=tuple(warnings))
