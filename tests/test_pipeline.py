import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.helpers import MockProvider, noisy_image, write_story
from weaver.compose import MissingArtwork, compose_page, export_pdf
from weaver.config import load_settings
from weaver.imaging import InvalidImage, verify_generated
from weaver.produce import PilotNotApproved, Producer
from weaver.quota import BudgetExceeded, Ledger, QuotaGuard, next_reset
from weaver.refs import ReferenceLibrary, load_specs
from weaver.state import LockedPanelError
from weaver.story import StoryError, load_characters, load_script, load_style


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        paths = write_story(self.root)
        self.settings = load_settings()
        self.lib = ReferenceLibrary(paths["refs_root"], load_specs(self.root / "references.yaml"))
        self.bible = load_characters(self.root / "characters.yaml")
        self.style = load_style(self.root / "style.yaml")
        self.script = load_script(self.root / "script.yaml", self.bible, set(self.lib.specs))
        self.workspace = self.root / "ws"

    def tearDown(self):
        self.tmp.cleanup()

    def producer(self, provider):
        return Producer(self.settings, self.workspace, self.script, self.bible, self.style, self.lib, provider)

    def test_produce_generates_versions_and_skips_existing(self):
        provider = MockProvider()
        prod = self.producer(provider)
        report = prod.run(["a1", "a2", "a3"])
        self.assertEqual(report.generated, ["a1 v1", "a2 v1", "a3 v1"])
        self.assertIsNone(report.stopped_reason)
        for pid in ("a1", "a2", "a3"):
            version = prod.state.selected_version(pid)
            self.assertTrue((self.workspace / version["file"]).exists())
        again = self.producer(provider).run(["a1", "a2", "a3"])
        self.assertEqual(again.generated, [])
        self.assertEqual(again.skipped, ["a1", "a2", "a3"])
        log = (self.workspace / "logs" / "generation.jsonl").read_text()
        self.assertEqual(log.count('"status": "ok"'), 3)

    def test_identity_refs_prefer_photos_and_prompt_indexes_match(self):
        prod = self.producer(MockProvider())
        plan = prod.plan(self.script.panel("a2"))
        self.assertEqual([s.ref_id for s in plan.slots], ["hero_face", "style", "hero_body"])
        self.assertIn("image 0 shows Hero's exact appearance", plan.prompt)
        self.assertIn("exact art style of image 1", plan.prompt)
        self.assertIn("image 2 shows Hero's outfit", plan.prompt)
        self.assertTrue(any("real photo not on disk" in w for w in plan.warnings))
        photos = self.lib.root / "photos"
        photos.mkdir()
        (photos / "hero.jpg").write_bytes(noisy_image(400, 500, 3, "JPEG"))
        plan = prod.plan(self.script.panel("a2"))
        self.assertEqual([s.ref_id for s in plan.slots], ["hero_photo", "hero_face", "hero_body"])
        self.assertEqual([s.purpose for s in plan.slots], ["identity", "drawn", "outfit"])
        self.assertIn("image 0 is a real photo of Hero: keep this exact face, face shape and skin tone", plan.prompt)
        self.assertIn("image 1 shows Hero as drawn in this comic", plan.prompt)
        self.assertIn("Draw the whole image in exactly the same art style as image 1", plan.prompt)
        self.assertFalse(plan.warnings)

    def test_quota_stop_saves_progress_and_resumes(self):
        prod = self.producer(MockProvider(fail_after=1))
        report = prod.run(["a1", "a2", "a3"])
        self.assertEqual(report.generated, ["a1 v1"])
        self.assertIn("exhausted", report.stopped_reason)
        self.assertTrue(prod.ledger.exhausted_today())
        blocked = self.producer(MockProvider()).run(["a2", "a3"])
        self.assertEqual(blocked.generated, [])
        self.assertIn("Resumes after", blocked.stopped_reason)
        # Next UTC day: the guard allows work again and only the remaining panels run.
        ledger = Ledger(self.workspace / "state" / "usage.json")
        tomorrow = next_reset()
        QuotaGuard(ledger, 9000).check(100, now=tomorrow)

    def test_budget_cap_blocks_before_request(self):
        provider = MockProvider(cost=4000)
        report = self.producer(provider).run(["a1", "a2", "a3"])
        self.assertEqual(len(provider.calls), 2)
        self.assertIn("would pass today's cap", report.stopped_reason)

    def test_guard_adds_spend_after_provider_report(self):
        ledger = Ledger(self.root / "usage.json")
        guard = QuotaGuard(ledger, 9000, provider_reported=8000)
        ledger.record(500)
        self.assertEqual(guard.used(), 8500)
        with self.assertRaises(BudgetExceeded):
            guard.check(600)

    def test_cap_above_free_allocation_is_refused(self):
        bad = self.root / "bad.yaml"
        text = (Path(__file__).resolve().parents[1] / "weaver.yaml").read_text()
        bad.write_text(text.replace("daily_neuron_cap: 9000", "daily_neuron_cap: 20000"))
        with self.assertRaises(ValueError):
            load_settings(bad)

    def test_pilot_gate(self):
        prod = self.producer(MockProvider())
        with self.assertRaises(PilotNotApproved):
            prod.run(["b1"])

    def test_regenerate_lock_and_tamper_detection(self):
        prod = self.producer(MockProvider())
        prod.run(["a1"])
        prod.generate_panel(self.script.panel("a1"), prod.guard())
        self.assertEqual(prod.state.selected_version("a1")["n"], 2)
        prod.state.select("a1", 1)
        prod.state.lock("a1")
        with self.assertRaises(LockedPanelError):
            prod.generate_panel(self.script.panel("a1"), prod.guard())
        self.assertEqual(prod.run(["a1"], regenerate=True).skipped, ["a1"])
        path = self.workspace / prod.state.selected_version("a1")["file"]
        path.write_bytes(noisy_image(50, 50, 99))
        with self.assertRaises(ValueError):
            prod.state.verify_locked("a1")

    def test_compose_and_pdf(self):
        prod = self.producer(MockProvider())
        with self.assertRaises(MissingArtwork):
            compose_page(self.script, 1, prod.state, self.settings.page, self.workspace / "pages")
        prod.run(["a1", "a2", "a3"])
        page = compose_page(self.script, 1, prod.state, self.settings.page, self.workspace / "pages")
        from PIL import Image
        with Image.open(page) as im:
            self.assertEqual(im.size, (self.settings.page["width"], self.settings.page["height"]))
        pdf = export_pdf([page, page], self.workspace / "out.pdf", 300)
        data = pdf.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertEqual(len(re.findall(rb"/Type\s*/Page\b", data)), 2)

    def test_output_verification(self):
        with self.assertRaises(InvalidImage):
            verify_generated(b"definitely not an image")
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (300, 300), (40, 40, 40)).save(buf, "PNG")
        with self.assertRaises(InvalidImage):
            verify_generated(buf.getvalue())
        self.assertEqual(verify_generated(noisy_image(300, 200))[:2], (300, 200))

    def test_script_validation(self):
        bad = self.root / "bad_script.yaml"
        bad.write_text((self.root / "script.yaml").read_text().replace("characters: [hero]}", "characters: [nobody]}", 1))
        with self.assertRaises(StoryError):
            load_script(bad, self.bible, set(self.lib.specs))

    def test_real_story_files_are_valid(self):
        bible = load_characters()
        specs = load_specs()
        script = load_script(characters=bible, ref_ids=set(specs))
        self.assertEqual(list(script.pilot_pages), [1, 2, 3])
        for char in bible.values():
            for r in char.refs:
                self.assertIn(r, specs, f"{char.id} references unknown ref {r}")


class ReferencePrepTests(unittest.TestCase):
    def test_masks_and_relative_boxes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "source").mkdir()
            (root / "source" / "a.png").write_bytes(noisy_image(1000, 800))
            specs_file = root / "refs.yaml"
            specs_file.write_text(
                "references:\n"
                "  r1: {source: source/a.png, box_rel: [0.0, 0.0, 0.5, 0.5], kind: art, role: identity}\n"
                "  r2: {source: source/a.png, box: [0, 0, 1000, 800], masks: [[0, 0, 100, 100]], kind: art, role: style}\n",
                encoding="utf-8")
            lib = ReferenceLibrary(root, load_specs(specs_file))
            self.assertEqual(lib.crop("r1").size, (500, 400))
            self.assertEqual(lib.crop("r2").getpixel((50, 50)), (255, 255, 255))
            prepared = lib.prepare("r2", 512)
            self.assertEqual((prepared.width, prepared.height), (512, 409))
            small = lib.prepare("r1", 1024)  # 500x400 crop is scaled up to the limit
            self.assertEqual((small.width, small.height), (1024, 819))


if __name__ == "__main__":
    unittest.main()
