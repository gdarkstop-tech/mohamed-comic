"""Test fixtures. Synthetic images here are TEST DATA only, never product output."""

from __future__ import annotations

import base64
import io
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw

from weaver.providers.base import GenerationRequest, GenerationResult, QuotaExhausted


def noisy_image(width: int, height: int, seed: int = 0, fmt: str = "PNG") -> bytes:
    rnd = random.Random(seed)
    im = Image.new("RGB", (width, height), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    draw = ImageDraw.Draw(im)
    for _ in range(40):
        x0, y0 = rnd.randrange(width), rnd.randrange(height)
        draw.ellipse((x0, y0, x0 + rnd.randrange(20, 200), y0 + rnd.randrange(20, 200)),
                     fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    buf = io.BytesIO()
    im.save(buf, format=fmt)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None, content: bytes = b"", content_type="application/json"):
        self.status_code = status
        self._body = body
        self.content = content or (json.dumps(body).encode() if body is not None else b"")
        self.text = self.content.decode(errors="replace")
        self.headers = {"content-type": content_type, "cf-ray": "test"}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Records requests and replays queued responses."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, headers=None, files=None, json=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "files": files, "json": json})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok_image_response(width=64 * 4, height=64 * 4, seed=1) -> FakeResponse:
    b64 = base64.b64encode(noisy_image(width, height, seed)).decode()
    return FakeResponse(200, {"success": True, "result": {"image": b64}, "errors": [], "messages": []})


def error_response(status: int, code: int, message: str) -> FakeResponse:
    return FakeResponse(status, {"success": False, "result": None, "errors": [{"code": code, "message": message}]})


class MockProvider:
    """In-memory provider for pipeline tests."""

    name = "mock"
    model = "mock-model"
    max_references = 4
    reference_max_side = 512

    def __init__(self, fail_after: int | None = None, cost: float = 100.0):
        self.calls: list[GenerationRequest] = []
        self.fail_after = fail_after
        self.cost = cost

    def estimate_cost(self, request):
        return self.cost

    def generate(self, request):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise QuotaExhausted("HTTP 429 code 3036: daily free allocation used", 3036, 429)
        self.calls.append(request)
        data = noisy_image(request.width // 4, request.height // 4, seed=len(self.calls))
        return GenerationResult(data, "png", self.name, self.model, self.cost, 1, {"uncertain_attempts": 0})

    def checks(self):
        return []

    def usage_today(self):
        return None


def write_story(root: Path) -> dict:
    """A tiny two-page story (pilot = page 1) with synthetic reference images."""
    (root / "refs" / "source").mkdir(parents=True)
    (root / "refs" / "source" / "sheet.png").write_bytes(noisy_image(900, 700, 7))
    (root / "references.yaml").write_text(
        "references:\n"
        "  hero_face: {source: source/sheet.png, box: [0, 0, 300, 300], kind: art, role: identity}\n"
        "  hero_photo: {source: photos/hero.*, box_rel: [0, 0, 1, 1], kind: photo, role: identity}\n"
        "  hero_body: {source: source/sheet.png, box: [300, 0, 900, 700], kind: art, role: outfit}\n"
        "  style: {source: source/sheet.png, box: [0, 300, 900, 700], kind: art, role: style}\n",
        encoding="utf-8")
    (root / "characters.yaml").write_text(
        "characters:\n"
        "  hero: {name: Hero, look: curly hair and glasses, outfit: beige shirt, refs: [hero_photo, hero_face, hero_body]}\n",
        encoding="utf-8")
    (root / "style.yaml").write_text(
        "style_ref: style\ndescription: inked comic\nno_text: No text.\nidentity_rule: Keep the face.\n", encoding="utf-8")
    (root / "script.yaml").write_text(
        "chapter: 1\ntitle: Test\nstatus: draft\npilot_pages: [1]\npages:\n"
        "  - number: 1\n    rows:\n      - {height: 1, panels: [a1]}\n      - {height: 1, panels: [a2, a3]}\n"
        "    panels:\n"
        "      - {id: a1, shot: wide, scene: a city at night, characters: [], lettering: [{type: caption, text: 'One night...', at: top-left}]}\n"
        "      - {id: a2, shot: medium, scene: hero walks, characters: [hero], lettering: [{type: speech, speaker: hero, text: 'Hello there, this is a test of a longer balloon.', at: top-right, tail: [0.5, 0.6]}]}\n"
        "      - {id: a3, shot: close, scene: hero thinks, characters: [hero], lettering: [{type: thought, speaker: hero, text: 'Hmm...', at: top-left, tail: [0.5, 0.6]}, {type: sfx, text: 'THWIP!', at: bottom-right}]}\n"
        "  - number: 2\n    rows:\n      - {height: 1, panels: [b1]}\n"
        "    panels:\n      - {id: b1, shot: wide, scene: later, characters: [hero]}\n",
        encoding="utf-8")
    return {"refs_root": root / "refs"}
