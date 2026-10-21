"""Resumable production state.

``production.json`` is the single source of truth for every panel: all
generated versions (with seed, prompt hash, reference hashes and cost), which
version is selected, and whether it is locked. It is rewritten atomically after
every change, so an interrupted run loses nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LockedPanelError(Exception):
    pass


class ProductionState:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.path = workspace / "state" / "production.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"version": 1, "pilot": {"approved": False, "approved_at": None}, "panels": {}}

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    # ----------------------------------------------------------------- panels
    def panel(self, panel_id: str) -> dict:
        return self.data["panels"].setdefault(
            panel_id,
            {"versions": [], "selected": None, "locked": False, "locked_sha256": None,
             "locked_at": None, "failures": []},
        )

    def has_panel(self, panel_id: str) -> bool:
        return bool(self.data["panels"].get(panel_id, {}).get("versions"))

    def is_locked(self, panel_id: str) -> bool:
        return bool(self.data["panels"].get(panel_id, {}).get("locked"))

    def next_version_number(self, panel_id: str) -> int:
        return len(self.panel(panel_id)["versions"]) + 1

    def add_version(self, panel_id: str, record: dict) -> dict:
        p = self.panel(panel_id)
        if p["locked"]:
            raise LockedPanelError(f"{panel_id} is locked; unlock it before regenerating.")
        p["versions"].append(record)
        p["selected"] = record["n"]
        self.save()
        return record

    def record_failure(self, panel_id: str, error: str) -> None:
        self.panel(panel_id)["failures"].append({"at": now_iso(), "error": error})
        self.save()

    def selected_version(self, panel_id: str) -> dict | None:
        p = self.data["panels"].get(panel_id)
        if not p or not p["selected"]:
            return None
        return next((v for v in p["versions"] if v["n"] == p["selected"]), None)

    def select(self, panel_id: str, n: int) -> None:
        p = self.panel(panel_id)
        if p["locked"]:
            raise LockedPanelError(f"{panel_id} is locked; unlock it before changing the selection.")
        if not any(v["n"] == n for v in p["versions"]):
            raise ValueError(f"{panel_id} has no version {n}")
        p["selected"] = n
        self.save()

    def lock(self, panel_id: str) -> dict:
        p = self.panel(panel_id)
        version = self.selected_version(panel_id)
        if version is None:
            raise ValueError(f"{panel_id} has no generated artwork to lock.")
        path = self.workspace / version["file"]
        digest = sha256_file(path)
        if digest != version["sha256"]:
            raise ValueError(f"{panel_id} v{version['n']} file changed on disk since generation; refusing to lock.")
        p.update(locked=True, locked_sha256=digest, locked_at=now_iso())
        self.save()
        return version

    def unlock(self, panel_id: str) -> None:
        p = self.panel(panel_id)
        p.update(locked=False, locked_sha256=None, locked_at=None)
        self.save()

    def verify_locked(self, panel_id: str) -> None:
        """Raise if a locked panel's artwork no longer matches its recorded hash."""
        p = self.data["panels"].get(panel_id) or {}
        if not p.get("locked"):
            return
        version = self.selected_version(panel_id)
        path = self.workspace / version["file"]
        if not path.exists() or sha256_file(path) != p["locked_sha256"]:
            raise ValueError(f"Locked artwork for {panel_id} is missing or was modified: {path}")

    # ------------------------------------------------------------------ pilot
    @property
    def pilot_approved(self) -> bool:
        return bool(self.data["pilot"]["approved"])

    def approve_pilot(self) -> None:
        self.data["pilot"] = {"approved": True, "approved_at": now_iso()}
        self.save()
