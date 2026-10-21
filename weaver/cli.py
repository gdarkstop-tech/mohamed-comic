"""Command line: python -m weaver <command>. Designed to be driven by Claude Code."""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
from pathlib import Path

from weaver import __version__
from weaver.config import ENV_FILE, FONTS_DIR, PRIVATE_DIR, REFERENCES_DIR, ROOT, load_settings, workspace_dir
from weaver.quota import Ledger, QuotaGuard, next_reset
from weaver.secrets import ACCOUNT_ID, API_TOKEN, env_file_is_ignored, get_credential, interactive_setup, mask_account_id

TEST_SCENES = {
    "mohamed": (
        "Mohamed sits on the stone ledge of a Cairo rooftop at night with his backpack beside him, "
        "the Nile and glowing city lights behind him and the Cairo Tower in the distance, a thoughtful half-smile"
    ),
}


def parse_pages(spec: str | None, default: list[int]) -> list[int]:
    if not spec:
        return list(default)
    pages: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(part))
    return sorted(pages)


def load_all():
    from weaver.refs import ReferenceLibrary
    from weaver.story import load_characters, load_script, load_style

    settings = load_settings()
    lib = ReferenceLibrary()
    bible = load_characters()
    style = load_style()
    script = load_script(characters=bible, ref_ids=set(lib.specs))
    return settings, lib, bible, style, script


def make_producer(with_provider: bool = True):
    from weaver.produce import Producer
    from weaver.providers import get_provider

    settings, lib, bible, style, script = load_all()
    provider = get_provider(settings) if with_provider else None
    return Producer(settings, workspace_dir(), script, bible, style, lib, provider)


def line(status: bool | None, name: str, detail: str) -> None:
    mark = {True: "PASS", False: "FAIL", None: "WARN"}[status]
    print(f"  [{mark}] {name}: {detail}")


# ------------------------------------------------------------------- commands
def cmd_doctor(args) -> int:
    print(f"WEAVER pipeline {__version__} — health check (secret values are never printed)\n")
    critical = False
    import PIL
    import requests
    import yaml
    line(True, "Python", f"{platform.python_version()} | Pillow {PIL.__version__} | requests {requests.__version__} | PyYAML {yaml.__version__}")
    try:
        settings = load_settings()
        line(True, "Settings", f"provider={settings.provider_name}, model={settings.model}, "
                               f"daily cap={settings.daily_neuron_cap:.0f}/{settings.free_allocation:.0f} free neurons")
    except Exception as exc:  # noqa: BLE001 - report any settings problem
        line(False, "Settings", str(exc))
        return 1

    ignored_env = env_file_is_ignored(ENV_FILE)
    ignored_private = env_file_is_ignored(PRIVATE_DIR / "x")
    line(ignored_env and ignored_private, "Git safety", f".env ignored={ignored_env}, private/ ignored={ignored_private}")
    critical |= not (ignored_env and ignored_private)

    account = get_credential(ACCOUNT_ID)
    token = get_credential(API_TOKEN)
    if account.value:
        valid = bool(re.fullmatch(r"[0-9a-f]{32}", account.value.reveal()))
        line(valid, "CLOUDFLARE_ACCOUNT_ID", f"set via {account.source} ({mask_account_id(account.value.reveal())})"
             + ("" if valid else " — expected 32 hex characters"))
    else:
        line(False, "CLOUDFLARE_ACCOUNT_ID", "missing")
    line(bool(token.value), "CLOUDFLARE_API_TOKEN", f"set via {token.source}" if token.value else "missing")

    ledger = Ledger(workspace_dir() / "state" / "usage.json")
    reported = None
    if account.value and token.value:
        from weaver.providers import get_provider

        provider = get_provider(settings)
        print("\n  Cloudflare (live):")
        for result in provider.checks():
            line(result.ok, result.name, result.detail)
            if result.name == "API token" and result.ok is False:
                critical = True
            if result.name == "Model availability" and result.ok is False:
                critical = True
            if result.name.startswith("Cloudflare-reported") and result.ok:
                reported = float(result.detail.split()[0])
        print()
    else:
        critical = True
        print("\n  Cloudflare checks skipped: credentials missing. See .env.example or CLAUDE.md > Credentials.\n")

    guard = QuotaGuard(ledger, settings.daily_neuron_cap, reported)
    exhausted = ledger.exhausted_today()
    line(not exhausted and guard.remaining() > 0, "Today's budget",
         f"{guard.used():.0f} used, {guard.remaining():.0f} remaining under the cap; "
         f"resets {next_reset().strftime('%Y-%m-%d 00:00 UTC')}" + (" — EXHAUSTED today" if exhausted else ""))

    try:
        _, lib, bible, _, script = load_all()
        available = [r for r in lib.specs if lib.available(r)]
        photos = [r for r in lib.specs if lib.specs[r].kind == "photo"]
        have_photos = [r for r in photos if lib.available(r)]
        line(bool(available), "References", f"{len(available)}/{len(lib.specs)} available; real photos: "
             f"{len(have_photos)}/{len(photos)}" + ("" if len(have_photos) == len(photos) else
             f" (missing: {', '.join(r.replace('_photo', '') for r in photos if r not in have_photos)})"))
        panels = script.all_panels()
        line(True, "Script", f"chapter {script.chapter} '{script.title}' [{script.status}] — "
                             f"{len(script.pages)} pages, {len(panels)} panels, pilot pages {list(script.pilot_pages)}")
    except Exception as exc:  # noqa: BLE001
        line(False, "Story files", str(exc))
        critical = True
    fonts = sorted(p.name for p in FONTS_DIR.glob("*.ttf"))
    line(len(fonts) >= 2, "Fonts", ", ".join(fonts))
    print("\nResult:", "NOT READY — fix the FAIL lines above." if critical else "READY")
    return 1 if critical else 0


def cmd_setup(args) -> int:
    if not sys.stdin.isatty():
        print("setup needs an interactive terminal (hidden input). From Claude Code, either:\n"
              "  * set CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN as environment secrets, or\n"
              "  * copy .env.example to .env and paste the two values into it in your editor.")
        return 2
    path = interactive_setup()
    print(f"Saved credentials to {path} (git-ignored, permissions 600). Run: python -m weaver doctor")
    return 0


def cmd_refs(args) -> int:
    from weaver.refs import ReferenceLibrary

    lib = ReferenceLibrary()
    if args.action == "list":
        for ref_id, spec in lib.specs.items():
            status = "ok     " if lib.available(ref_id) else "MISSING"
            print(f"  {status} {ref_id:24s} {spec.kind:5s} {spec.role:9s} {spec.source}")
        return 0
    if args.action == "preview":
        ids = args.ids.split(",") if args.ids else list(lib.specs)
        out = Path(args.out) if args.out else workspace_dir() / "review" / "references.png"
        print(f"Wrote {lib.contact_sheet(ids, out)}")
        return 0
    if args.action == "import":
        if not args.file or not args.character:
            print("usage: python -m weaver refs import FILE --as CHARACTER")
            return 2
        dest = lib.import_photo(Path(args.file), args.character)
        print(f"Imported {args.file} -> {dest.relative_to(ROOT)} (git-ignored). "
              f"Check the crop: python -m weaver refs preview --ids {args.character}_photo")
        return 0
    return 2


def _print_plan(producer, panel_ids: list[str]) -> float:
    from weaver.providers.cloudflare import NEURONS_PER_INPUT_TILE, NEURONS_PER_OUTPUT_TILE, tiles
    from weaver.imaging import fit_within

    total = 0.0
    for pid in panel_ids:
        panel = producer.script.panel(pid)
        plan = producer.plan(panel)
        w, h = producer.panel_size(panel)
        ref_tiles = 0
        for slot in plan.slots:
            im = fit_within(producer.lib.crop(slot.ref_id), producer.settings.ref_max_side)
            ref_tiles += tiles(im.width, im.height)
        cost = tiles(w, h) * NEURONS_PER_OUTPUT_TILE + ref_tiles * NEURONS_PER_INPUT_TILE
        total += cost
        state = producer.state
        tag = "LOCKED" if state.is_locked(pid) else ("has art" if state.has_panel(pid) else "pending")
        print(f"  {pid:6s} {w}x{h}  ~{cost:5.0f} neurons  [{tag}]  refs: {', '.join(s.ref_id for s in plan.slots) or '-'}")
        for warning in plan.warnings:
            print(f"         ! {warning}")
    return total


def cmd_plan(args) -> int:
    producer = make_producer(with_provider=False)
    pages = parse_pages(args.pages, list(producer.script.pilot_pages))
    ids = [p.id for n in pages for p in producer.script.page(n).panels]
    print(f"Plan for pages {pages} (no API calls):")
    total = _print_plan(producer, ids)
    guard = QuotaGuard(producer.ledger, producer.settings.daily_neuron_cap)
    print(f"\nEstimated total ~{total:.0f} neurons for one version of every panel listed. "
          f"Today's local budget remaining: {guard.remaining():.0f}.")
    if args.show_prompts:
        for pid in ids:
            print(f"\n--- {pid}\n{producer.plan(producer.script.panel(pid)).prompt}")
    return 0


def cmd_test(args) -> int:
    from weaver.providers.base import ProviderError
    from weaver.quota import BudgetExceeded

    character = args.character
    if character not in TEST_SCENES and not args.scene:
        print(f"No default test scene for '{character}'. Pass --scene \"...\".")
        return 2
    width, height = (int(v) for v in args.size.lower().split("x"))
    producer = make_producer()
    try:
        meta = producer.character_test(character, args.scene or TEST_SCENES[character], width, height, args.seed)
    except BudgetExceeded as exc:
        print(f"Not started: {exc}")
        return 3
    except ProviderError as exc:
        print(f"Generation failed ({type(exc).__name__}): {exc}")
        return 1
    print("Test image generated and verified (decodes, non-blank):")
    print(f"  file:    {meta['file']}")
    print(f"  size:    {meta['size'][0]}x{meta['size'][1]}   seed: {meta['seed']}   sha256: {meta['sha256'][:16]}…")
    print(f"  model:   {meta['provider']} {meta['model']}   ~{meta['neurons_charged']:.0f} neurons")
    print(f"  refs:    " + ", ".join(f"image {i} = {r['id']} ({r['purpose']})" for i, r in enumerate(meta["refs"])))
    for warning in meta["warnings"]:
        print(f"  note:    {warning}")
    return 0


def _report(report) -> int:
    for item in report.generated:
        print(f"  generated {item}")
    if report.skipped:
        print(f"  skipped (already has art or locked): {', '.join(report.skipped)}")
    for pid, err in report.failed.items():
        print(f"  FAILED {pid}: {err}")
    print(f"  ~{report.neurons_estimated:.0f} neurons charged this run")
    if report.stopped_reason:
        print(f"\nStopped: {report.stopped_reason}")
        return 3
    return 1 if report.failed else 0


def cmd_produce(args) -> int:
    producer = make_producer()
    if args.panels:
        ids = args.panels.split(",")
    else:
        pages = parse_pages(args.pages, list(producer.script.pilot_pages))
        ids = [p.id for n in pages for p in producer.script.page(n).panels]
    print(f"Producing {len(ids)} panel(s): {', '.join(ids)}")
    return _report(producer.run(ids, regenerate=False, max_requests=args.max))


def cmd_regen(args) -> int:
    producer = make_producer()
    from weaver.quota import BudgetExceeded
    from weaver.providers.base import ProviderError
    from weaver.state import LockedPanelError

    panel = producer.script.panel(args.panel)
    producer.check_scope([panel.page])
    from weaver.quota import RunLock

    try:
        with RunLock(producer.workspace / "state" / "run.lock"):
            record = producer.generate_panel(panel, producer.guard(), seed=args.seed)
    except (BudgetExceeded, LockedPanelError) as exc:
        print(f"Not started: {exc}")
        return 3
    except ProviderError as exc:
        producer.state.record_failure(panel.id, str(exc))
        print(f"Failed ({type(exc).__name__}): {exc}")
        return 1
    print(f"Generated {panel.id} v{record['n']} -> {producer.workspace / record['file']} (now selected)")
    return 0


def cmd_select(args) -> int:
    from weaver.state import ProductionState

    ProductionState(workspace_dir()).select(args.panel, args.version)
    print(f"{args.panel}: selected v{args.version}")
    return 0


def cmd_lock(args) -> int:
    from weaver.state import ProductionState

    state = ProductionState(workspace_dir())
    ids = list(args.panels)
    if args.page:
        _, _, _, _, script = load_all()
        ids += [p.id for p in script.page(args.page).panels]
    for pid in ids:
        version = state.lock(pid)
        print(f"  locked {pid} v{version['n']} (sha256 {version['sha256'][:12]}…)")
    return 0


def cmd_unlock(args) -> int:
    from weaver.state import ProductionState

    if not args.confirm:
        print("Unlocking allows approved artwork to be replaced. Re-run with --confirm if the author asked for it.")
        return 2
    ProductionState(workspace_dir()).unlock(args.panel)
    print(f"{args.panel}: unlocked")
    return 0


def cmd_status(args) -> int:
    from weaver.state import ProductionState

    _, lib, _, _, script = load_all()
    settings = load_settings()
    state = ProductionState(workspace_dir())
    ledger = Ledger(workspace_dir() / "state" / "usage.json")
    print(f"Chapter {script.chapter}: {script.title} [{script.status}]  pilot approved: {state.pilot_approved}")
    for page in script.pages:
        print(f"\n Page {page.number}{' (pilot)' if page.number in script.pilot_pages else ''}")
        for panel in page.panels:
            p = state.data["panels"].get(panel.id, {})
            versions = len(p.get("versions", []))
            sel = p.get("selected")
            lock = "LOCKED" if p.get("locked") else ""
            fail = f" last error: {p['failures'][-1]['error'][:70]}" if p.get("failures") else ""
            print(f"   {panel.id:6s} versions={versions} selected={sel or '-'} {lock}{fail}")
    guard = QuotaGuard(ledger, settings.daily_neuron_cap)
    print(f"\nToday (UTC): ~{guard.used():.0f} neurons used by this tool, {guard.remaining():.0f} left under the "
          f"{settings.daily_neuron_cap:.0f} cap" + (" — Cloudflare reported EXHAUSTED" if ledger.exhausted_today() else ""))
    return 0


def cmd_show(args) -> int:
    producer = make_producer(with_provider=False)
    panel = producer.script.panel(args.panel)
    plan = producer.plan(panel)
    print(f"{panel.id} (page {panel.page}) size {producer.panel_size(panel)}")
    for i, slot in enumerate(plan.slots):
        print(f"  image {i}: {slot.ref_id} ({slot.purpose}{', ' + slot.character if slot.character else ''})")
    print(f"\nPrompt:\n{plan.prompt}\n")
    print(json.dumps(producer.state.data["panels"].get(panel.id, {}), indent=2))
    return 0


def cmd_compose(args) -> int:
    from weaver.compose import MissingArtwork, compose_page
    from weaver.state import ProductionState

    settings, _, _, _, script = load_all()
    state = ProductionState(workspace_dir())
    pages = parse_pages(args.pages, list(script.pilot_pages))
    status = 0
    for n in pages:
        try:
            out = compose_page(script, n, state, settings.page, workspace_dir() / "pages")
            print(f"  page {n} -> {out}")
        except MissingArtwork as exc:
            print(f"  {exc}")
            status = 1
    return status


def cmd_export(args) -> int:
    from weaver.compose import MissingArtwork, compose_page, export_pdf
    from weaver.state import ProductionState

    settings, _, _, _, script = load_all()
    state = ProductionState(workspace_dir())
    pages = parse_pages(args.pages, list(script.pilot_pages))
    try:
        files = [compose_page(script, n, state, settings.page, workspace_dir() / "pages") for n in pages]
    except MissingArtwork as exc:
        print(f"Not exported: {exc}")
        return 1
    out = Path(args.out) if args.out else workspace_dir() / "export" / f"weaver-chapter-{script.chapter:02d}-pages-{pages[0]}-{pages[-1]}.pdf"
    export_pdf(files, out, int(settings.page["dpi"]))
    print(f"PDF written: {out} ({len(files)} pages)")
    return 0


def cmd_approve_pilot(args) -> int:
    from weaver.state import ProductionState

    _, _, _, _, script = load_all()
    state = ProductionState(workspace_dir())
    if not args.confirm:
        print("Only run this when the author has explicitly approved the 3-page pilot. Re-run with --confirm.")
        return 2
    unlocked = [p.id for n in script.pilot_pages for p in script.page(n).panels if not state.is_locked(p.id)]
    if unlocked:
        print(f"Pilot cannot be approved: these pilot panels are not locked yet: {', '.join(unlocked)}")
        return 1
    state.approve_pilot()
    print("Pilot approved. Pages beyond the pilot may now be produced.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m weaver", description="WEAVER comic production pipeline")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check credentials, Cloudflare access, quota, references").set_defaults(fn=cmd_doctor)
    sub.add_parser("setup", help="enter Cloudflare credentials (hidden input) into .env").set_defaults(fn=cmd_setup)

    r = sub.add_parser("refs", help="reference images: list | preview | import")
    r.add_argument("action", choices=["list", "preview", "import"])
    r.add_argument("file", nargs="?")
    r.add_argument("--as", dest="character")
    r.add_argument("--ids")
    r.add_argument("--out")
    r.set_defaults(fn=cmd_refs)

    for name in ("test-character", "test-mohamed"):
        t = sub.add_parser(name, help="generate ONE identity/style test image")
        if name == "test-character":
            t.add_argument("character")
        else:
            t.set_defaults(character="mohamed")
        t.add_argument("--scene")
        t.add_argument("--size", default="1024x1024")
        t.add_argument("--seed", type=int, default=20261021)
        t.set_defaults(fn=cmd_test)

    p = sub.add_parser("plan", help="show refs, sizes and cost estimate (no API calls)")
    p.add_argument("--pages")
    p.add_argument("--show-prompts", action="store_true")
    p.set_defaults(fn=cmd_plan)

    pr = sub.add_parser("produce", help="generate panels that have no artwork yet (pilot pages by default)")
    pr.add_argument("--pages")
    pr.add_argument("--panels")
    pr.add_argument("--max", type=int)
    pr.set_defaults(fn=cmd_produce)

    rg = sub.add_parser("regen", help="generate a new version of one panel")
    rg.add_argument("panel")
    rg.add_argument("--seed", type=int)
    rg.set_defaults(fn=cmd_regen)

    se = sub.add_parser("select", help="choose which version of a panel is used")
    se.add_argument("panel")
    se.add_argument("version", type=int)
    se.set_defaults(fn=cmd_select)

    lk = sub.add_parser("lock", help="freeze approved artwork (hash-verified)")
    lk.add_argument("panels", nargs="*")
    lk.add_argument("--page", type=int)
    lk.set_defaults(fn=cmd_lock)

    ul = sub.add_parser("unlock", help="unfreeze a panel (author request only)")
    ul.add_argument("panel")
    ul.add_argument("--confirm", action="store_true")
    ul.set_defaults(fn=cmd_unlock)

    sub.add_parser("status", help="production status and today's quota").set_defaults(fn=cmd_status)

    sh = sub.add_parser("show", help="show a panel's prompt, references and versions")
    sh.add_argument("panel")
    sh.set_defaults(fn=cmd_show)

    co = sub.add_parser("compose", help="compose lettered pages from selected artwork")
    co.add_argument("--pages")
    co.set_defaults(fn=cmd_compose)

    ex = sub.add_parser("export-pdf", help="compose pages and export a PDF")
    ex.add_argument("--pages")
    ex.add_argument("--out")
    ex.set_defaults(fn=cmd_export)

    ap_ = sub.add_parser("approve-pilot", help="record the author's approval of the pilot")
    ap_.add_argument("--confirm", action="store_true")
    ap_.set_defaults(fn=cmd_approve_pilot)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted. State is saved after every panel; re-run to resume.")
        return 130
    except Exception as exc:  # noqa: BLE001 - one clean line instead of a traceback
        from weaver.providers.base import AuthError

        print(f"Error ({type(exc).__name__}): {exc}")
        return 1 if not isinstance(exc, AuthError) else 4
