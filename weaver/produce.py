"""Panel production: generate, verify, version, and stop cleanly at the quota.

Every request passes the quota guard first. Every outcome (success or error)
is appended to an audit log. State is saved after each panel, so re-running
``produce`` after a quota stop simply continues with the remaining panels.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from weaver.config import Settings
from weaver.imaging import InvalidImage, generation_size, verify_generated
from weaver.layout import cell_aspect, page_cells
from weaver.prompting import PanelPlan, plan_panel
from weaver.providers.base import (
    AuthError, GenerationRequest, ImageProvider, InvalidRequest, PlanRequired, ProviderError, QuotaExhausted,
)
from weaver.quota import BudgetExceeded, Ledger, QuotaGuard, RunLock, utc_now
from weaver.refs import ReferenceLibrary
from weaver.state import LockedPanelError, ProductionState, now_iso, sha256_bytes
from weaver.story import Character, Panel, Script, Style


class PilotNotApproved(Exception):
    pass


@dataclass
class RunReport:
    generated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    stopped_reason: str | None = None
    neurons_estimated: float = 0.0


def derive_seed(key: str, version: int) -> int:
    return int(hashlib.sha256(f"{key}:{version}".encode()).hexdigest()[:8], 16) % 2_147_483_647


class Producer:
    def __init__(self, settings: Settings, workspace: Path, script: Script, bible: dict[str, Character],
                 style: Style, lib: ReferenceLibrary, provider: ImageProvider | None):
        self.settings = settings
        self.workspace = workspace
        self.script = script
        self.bible = bible
        self.style = style
        self.lib = lib
        self.provider = provider
        self.state = ProductionState(workspace)
        self.ledger = Ledger(workspace / "state" / "usage.json")
        self.audit_path = workspace / "logs" / "generation.jsonl"

    # ---------------------------------------------------------------- planning
    def panel_size(self, panel: Panel) -> tuple[int, int]:
        rect = page_cells(self.script.page(panel.page), self.settings.page)[panel.id]
        s = self.settings
        return generation_size(cell_aspect(rect), s.target_megapixels, s.gen_min_side, s.gen_max_side, s.gen_multiple)

    def plan(self, panel: Panel) -> PanelPlan:
        return plan_panel(panel, self.bible, self.style, self.lib, self.settings.refs_per_request)

    def build_request(self, plan: PanelPlan, width: int, height: int, seed: int) -> GenerationRequest:
        refs = tuple(self.lib.prepare(s.ref_id, self.settings.ref_max_side) for s in plan.slots)
        return GenerationRequest(prompt=plan.prompt, width=width, height=height, references=refs, seed=seed)

    def check_scope(self, pages: list[int]) -> None:
        outside = [p for p in pages if p not in self.script.pilot_pages]
        if outside and not self.state.pilot_approved:
            raise PilotNotApproved(
                f"Pages {outside} are outside the pilot {list(self.script.pilot_pages)}. "
                "The author must approve the pilot first (python -m weaver approve-pilot)."
            )

    def guard(self) -> QuotaGuard:
        reported = self.provider.usage_today() if self.provider else None
        return QuotaGuard(self.ledger, self.settings.daily_neuron_cap, reported)

    # ------------------------------------------------------------- generation
    def _audit(self, record: dict) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _call(self, request: GenerationRequest, guard: QuotaGuard, context: dict):
        """One guarded provider call. Charges the ledger conservatively either way."""
        estimate = self.provider.estimate_cost(request)
        guard.check(estimate)
        base = {
            "at": now_iso(), **context, "provider": self.provider.name, "model": self.provider.model,
            "width": request.width, "height": request.height, "seed": request.seed,
            "prompt_sha256": sha256_bytes(request.prompt.encode()),
            "refs": [{"id": r.ref_id, "sha256": r.sha256, "size": [r.width, r.height]} for r in request.references],
            "neurons_estimated": estimate,
        }
        try:
            result = self.provider.generate(request)
        except QuotaExhausted as exc:
            self.ledger.mark_exhausted()
            if exc.uncertain_attempts:
                self.ledger.record(estimate * exc.uncertain_attempts)
            self._audit({**base, "status": "quota_exhausted", "error": str(exc)})
            raise
        except ProviderError as exc:
            if exc.uncertain_attempts:
                self.ledger.record(estimate * exc.uncertain_attempts)
            self._audit({**base, "status": "error", "error": str(exc), "code": exc.code})
            raise
        charged = estimate * (1 + int(result.meta.get("uncertain_attempts", 0)))
        self.ledger.record(charged)
        try:
            width, height, fmt = verify_generated(result.image_bytes)
        except InvalidImage as exc:
            self._audit({**base, "status": "invalid_output", "error": str(exc)})
            raise
        digest = sha256_bytes(result.image_bytes)
        self._audit({**base, "status": "ok", "output_sha256": digest, "output_size": [width, height],
                     "attempts": result.attempts, "neurons_charged": charged})
        return result, width, height, fmt, digest, charged

    def generate_panel(self, panel: Panel, guard: QuotaGuard, seed: int | None = None) -> dict:
        if self.state.is_locked(panel.id):
            raise LockedPanelError(f"{panel.id} is locked")
        n = self.state.next_version_number(panel.id)
        seed = seed if seed is not None else (panel.seed if panel.seed is not None and n == 1 else derive_seed(panel.id, n))
        plan = self.plan(panel)
        width, height = self.panel_size(panel)
        request = self.build_request(plan, width, height, seed)
        result, w, h, fmt, digest, charged = self._call(request, guard, {"panel": panel.id, "version": n})
        rel = Path("panels") / panel.id / f"v{n}.{ 'jpg' if fmt == 'jpeg' else fmt }"
        out = self.workspace / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(result.image_bytes)
        (out.with_suffix(".prompt.txt")).write_text(plan.prompt + "\n", encoding="utf-8")
        record = {
            "n": n, "file": rel.as_posix(), "sha256": digest, "width": w, "height": h, "seed": seed,
            "prompt_sha256": sha256_bytes(plan.prompt.encode()), "refs": [s.ref_id for s in plan.slots],
            "provider": result.provider, "model": result.model, "neurons_charged": charged,
            "created_at": now_iso(), "warnings": list(plan.warnings),
        }
        return self.state.add_version(panel.id, record)

    def run(self, panel_ids: list[str], regenerate: bool = False, max_requests: int | None = None) -> RunReport:
        if self.provider is None:
            raise AuthError("No image provider configured.")
        report = RunReport()
        pages = sorted({self.script.panel(pid).page for pid in panel_ids})
        self.check_scope(pages)
        with RunLock(self.workspace / "state" / "run.lock"):
            guard = self.guard()
            for pid in panel_ids:
                panel = self.script.panel(pid)
                if self.state.is_locked(pid) or (self.state.has_panel(pid) and not regenerate):
                    report.skipped.append(pid)
                    continue
                if max_requests is not None and len(report.generated) >= max_requests:
                    report.stopped_reason = f"reached --max {max_requests}"
                    break
                try:
                    record = self.generate_panel(panel, guard)
                    report.generated.append(f"{pid} v{record['n']}")
                    report.neurons_estimated += record["neurons_charged"]
                except BudgetExceeded as exc:
                    report.stopped_reason = str(exc)
                    break
                except QuotaExhausted as exc:
                    report.stopped_reason = f"Cloudflare free allocation exhausted ({exc}). Progress saved; run again after 00:00 UTC."
                    break
                except (AuthError, PlanRequired) as exc:
                    report.stopped_reason = f"{type(exc).__name__}: {exc}"
                    break
                except (InvalidRequest, InvalidImage) as exc:
                    self.state.record_failure(pid, str(exc))
                    report.failed[pid] = str(exc)
                except ProviderError as exc:
                    self.state.record_failure(pid, str(exc))
                    report.failed[pid] = str(exc)
                    report.stopped_reason = f"provider error after retries: {exc}. Progress saved; re-run to resume."
                    break
        return report

    # ------------------------------------------------------------ test image
    def character_test(self, character_id: str, scene: str, width: int, height: int, seed: int) -> dict:
        """One identity/style test image for a character, through the same guard and ledger."""
        panel = Panel(id=f"test-{character_id}", page=0, shot="medium shot, three-quarter view",
                      scene=scene, characters=(character_id,), setting=True)
        plan = self.plan(panel)
        request = self.build_request(plan, width, height, seed)
        with RunLock(self.workspace / "state" / "run.lock"):
            guard = self.guard()
            result, w, h, fmt, digest, charged = self._call(request, guard, {"test": character_id})
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        out = self.workspace / "tests" / f"{character_id}-{stamp}.{ 'jpg' if fmt == 'jpeg' else fmt }"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(result.image_bytes)
        meta = {"file": str(out), "sha256": digest, "size": [w, h], "seed": seed, "neurons_charged": charged,
                "provider": result.provider, "model": result.model, "prompt": plan.prompt,
                "refs": [{"id": s.ref_id, "purpose": s.purpose} for s in plan.slots], "warnings": list(plan.warnings)}
        out.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return meta
