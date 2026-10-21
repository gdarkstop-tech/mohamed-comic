import unittest

import requests

from tests.helpers import FakeResponse, FakeSession, error_response, noisy_image, ok_image_response
from weaver.imaging import fit_within, load_rgb
from weaver.providers.base import (
    AuthError, GenerationRequest, InvalidRequest, PlanRequired, QuotaExhausted, ReferenceInput, TransientError,
)
from weaver.providers.cloudflare import CloudflareWorkersAI, tiles
from weaver.secrets import Secret

ACCOUNT = "0123456789abcdef0123456789abcdef"
TOKEN = "tok_SUPER_SECRET_VALUE_123"
MODEL = "@cf/black-forest-labs/flux-2-klein-4b"


def provider(responses, retries=2):
    session = FakeSession(responses)
    p = CloudflareWorkersAI(Secret(ACCOUNT), Secret(TOKEN), MODEL, max_retries=retries,
                            session=session, sleep=lambda s: None)
    return p, session


def ref(ref_id="face", w=300, h=400):
    return ReferenceInput(ref_id, noisy_image(w, h), w, h, "x" * 64)


class CloudflareProviderTests(unittest.TestCase):
    def test_multipart_contract(self):
        p, session = provider([ok_image_response()])
        req = GenerationRequest("a hero", 1024, 768, (ref("a"), ref("b"), ref("c"), ref("d")), seed=7)
        result = p.generate(req)
        call = session.calls[0]
        self.assertEqual(call["url"], f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/run/{MODEL}")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {TOKEN}")
        files = call["files"]
        self.assertEqual(files["prompt"], (None, "a hero"))
        self.assertEqual(files["width"], (None, "1024"))
        self.assertEqual(files["height"], (None, "768"))
        self.assertEqual(files["seed"], (None, "7"))
        for i, name in enumerate("abcd"):
            self.assertEqual(files[f"input_image_{i}"][0], f"{name}.png")
            self.assertEqual(files[f"input_image_{i}"][2], "image/png")
        self.assertEqual(result.image_format, "png")
        self.assertEqual(result.attempts, 1)

    def test_cost_estimate_matches_published_rates(self):
        p, _ = provider([])
        req = GenerationRequest("x", 1024, 1024, (ref(w=512, h=512),))
        self.assertAlmostEqual(p.estimate_cost(req), 4 * 26.05 + 1 * 5.37, places=2)
        self.assertEqual(tiles(1312, 768), 6)

    def test_klein_9b_megapixel_pricing_and_unknown_models_refused(self):
        from weaver.providers.cloudflare import KLEIN_9B, estimate_neurons
        self.assertAlmostEqual(estimate_neurons(KLEIN_9B, 1024, 1024, [(512, 512)] * 3), 1363.64 + 3 * 181.82, places=2)
        self.assertAlmostEqual(estimate_neurons(KLEIN_9B, 1280, 1024, []), 1363.64 + 181.82, places=2)
        with self.assertRaises(InvalidRequest):
            estimate_neurons("@cf/some/unpriced-model", 512, 512, [])
        with self.assertRaises(InvalidRequest):
            CloudflareWorkersAI(Secret(ACCOUNT), Secret(TOKEN), "@cf/some/unpriced-model")

    def test_rejects_more_than_four_refs_and_oversized_refs(self):
        p, session = provider([])
        with self.assertRaises(InvalidRequest):
            p.generate(GenerationRequest("x", 512, 512, tuple(ref(str(i)) for i in range(5))))
        with self.assertRaises(InvalidRequest):
            p.generate(GenerationRequest("x", 512, 512, (ref(w=600, h=300),)))
        with self.assertRaises(InvalidRequest):
            p.generate(GenerationRequest("x", 2048, 512))
        self.assertEqual(session.calls, [])

    def test_quota_exhausted_is_not_retried(self):
        p, session = provider([error_response(429, 3036, "You have used up your daily free allocation of 10,000 neurons.")])
        with self.assertRaises(QuotaExhausted):
            p.generate(GenerationRequest("x", 512, 512))
        self.assertEqual(len(session.calls), 1)

    def test_capacity_error_is_retried(self):
        p, session = provider([error_response(429, 3040, "Capacity temporarily exceeded"), ok_image_response()])
        result = p.generate(GenerationRequest("x", 512, 512))
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(session.calls), 2)

    def test_timeouts_count_as_uncertain_spend(self):
        p, _ = provider([requests.Timeout("slow"), ok_image_response()])
        result = p.generate(GenerationRequest("x", 512, 512))
        self.assertEqual(result.meta["uncertain_attempts"], 1)

    def test_retries_are_bounded(self):
        p, session = provider([error_response(503, 0, "down")] * 3, retries=2)
        with self.assertRaises(TransientError):
            p.generate(GenerationRequest("x", 512, 512))
        self.assertEqual(len(session.calls), 3)

    def test_auth_and_plan_errors(self):
        p, _ = provider([error_response(401, 10000, "Authentication error")])
        with self.assertRaises(AuthError):
            p.generate(GenerationRequest("x", 512, 512))
        p, _ = provider([error_response(403, 5035, "This model requires a Workers Paid plan.")])
        with self.assertRaises(PlanRequired):
            p.generate(GenerationRequest("x", 512, 512))

    def test_errors_never_contain_secrets(self):
        p, _ = provider([error_response(400, 5007, f"No such model for {ACCOUNT} token {TOKEN}")])
        with self.assertRaises(InvalidRequest) as ctx:
            p.generate(GenerationRequest("x", 512, 512))
        message = str(ctx.exception)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn(ACCOUNT, message)
        self.assertIn("…cdef", message)

    def test_non_image_payload_is_rejected(self):
        import base64
        bad = FakeResponse(200, {"success": True, "result": {"image": base64.b64encode(b"not an image").decode()}})
        p, _ = provider([bad])
        with self.assertRaises(Exception):
            p.generate(GenerationRequest("x", 512, 512))

    def test_secret_repr_is_masked(self):
        s = Secret(TOKEN)
        self.assertNotIn(TOKEN, repr(s))
        self.assertNotIn(TOKEN, str(s))
        self.assertNotIn(TOKEN, f"{s}")

    def test_reference_resizing_fits_provider_limit(self):
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(noisy_image(1536, 1024)))
        fitted = fit_within(im, 512)
        self.assertLessEqual(max(fitted.size), 512)
        self.assertEqual(fitted.size, (512, 341))
        small = fit_within(Image.open(io.BytesIO(noisy_image(200, 100))), 512)
        self.assertEqual(small.size, (200, 100))


if __name__ == "__main__":
    unittest.main()
