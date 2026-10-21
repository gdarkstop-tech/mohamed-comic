"""Paths and non-secret settings.

Everything private (reference photos, approved artwork, generated output,
production state) lives under ``private/`` which is git-ignored, because the
repository is public and the material depicts real people.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "weaver.yaml"
STORY_DIR = ROOT / "story"
ASSETS_DIR = ROOT / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
ENV_FILE = ROOT / ".env"

PRIVATE_DIR = ROOT / "private"
REFERENCES_DIR = PRIVATE_DIR / "references"


def workspace_dir() -> Path:
    """Where state, panels, pages and logs are written. Overridable for tests."""
    override = os.environ.get("WEAVER_WORKSPACE")
    return Path(override).resolve() if override else PRIVATE_DIR / "workspace"


@dataclass(frozen=True)
class Settings:
    provider_name: str
    model: str
    request_timeout_s: float
    max_retries: int
    free_allocation: float
    daily_neuron_cap: float
    ref_max_side: int
    refs_per_request: int
    target_megapixels: float
    gen_min_side: int
    gen_max_side: int
    gen_multiple: int
    page: dict[str, Any]


def load_settings(path: Path = SETTINGS_FILE) -> Settings:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    p, q, r, g = data["provider"], data["quota"], data["references"], data["generation"]
    cap = float(q["daily_neuron_cap"])
    free = float(q["free_allocation"])
    if cap > free:
        # Deterministic guard: configuration can never authorise billable overage.
        raise ValueError(
            f"quota.daily_neuron_cap ({cap}) exceeds the free allocation ({free}); refusing to run."
        )
    return Settings(
        provider_name=p["name"],
        model=p["model"],
        request_timeout_s=float(p.get("request_timeout_s", 180)),
        max_retries=int(p.get("max_retries", 3)),
        free_allocation=free,
        daily_neuron_cap=cap,
        ref_max_side=int(r["max_side"]),
        refs_per_request=int(r["max_per_request"]),
        target_megapixels=float(g["target_megapixels"]),
        gen_min_side=int(g["min_side"]),
        gen_max_side=int(g["max_side"]),
        gen_multiple=int(g["multiple_of"]),
        page=dict(data["page"]),
    )
