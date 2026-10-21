"""Cloudflare Workers AI provider (official REST API).

Contract (developers.cloudflare.com, FLUX.2 [klein] 4B):
  POST /client/v4/accounts/{account_id}/ai/run/@cf/black-forest-labs/flux-2-klein-4b
  multipart/form-data: prompt (required), width/height (256-1920), seed, guidance,
  input_image_0..input_image_3 (binary, each within 512x512). Steps are fixed at 4.
  Response: {"result": {"image": "<base64>"}, "success": true, ...}

Pricing (Workers AI pricing page, checked 2026-10-21) is per model, see PRICING.
Free plan: 10,000 neurons per UTC day; exceeding it returns internal error
3036 / HTTP 429. A model without a known price is refused, so the quota guard
can never under-count.
"""

from __future__ import annotations

import base64
import binascii
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

from weaver.providers.base import (
    AuthError,
    CheckResult,
    GenerationRequest,
    GenerationResult,
    InvalidRequest,
    PlanRequired,
    ProviderError,
    QuotaExhausted,
    TransientError,
)
from weaver.secrets import Secret, mask_account_id, redact

API_BASE = "https://api.cloudflare.com/client/v4"
TILE = 512
MEGAPIXEL = 1024 * 1024
KLEIN_4B = "@cf/black-forest-labs/flux-2-klein-4b"
KLEIN_9B = "@cf/black-forest-labs/flux-2-klein-9b"
# klein-4b: neurons per 512x512 tile.  klein-9b: neurons per megapixel (1024x1024).
NEURONS_PER_INPUT_TILE = 5.37
NEURONS_PER_OUTPUT_TILE = 26.05
KLEIN_9B_FIRST_MP = 1363.64
KLEIN_9B_EXTRA_MP = 181.82
KLEIN_9B_INPUT_MP = 181.82

QUOTA_CODES = {3036}
TRANSIENT_CODES = {3040, 3007, 3008}
PLAN_CODES = {5035}
AUTH_CODES = {10000, 10001, 9109, 1000, 1001, 3023}
INVALID_CODES = {5007, 3042, 3006, 3003, 5004}


def tiles(width: int, height: int) -> int:
    return math.ceil(width / TILE) * math.ceil(height / TILE)


def estimate_neurons(model: str, width: int, height: int, ref_sizes: list[tuple[int, int]]) -> float:
    """Conservative cost from the published per-model rates (partial units round up)."""
    if model == KLEIN_4B:
        out = tiles(width, height) * NEURONS_PER_OUTPUT_TILE
        inp = sum(tiles(w, h) for w, h in ref_sizes) * NEURONS_PER_INPUT_TILE
    elif model == KLEIN_9B:
        extra_mp = math.ceil(max(0.0, width * height / MEGAPIXEL - 1.0))
        out = KLEIN_9B_FIRST_MP + extra_mp * KLEIN_9B_EXTRA_MP
        inp = sum(math.ceil(w * h / MEGAPIXEL) for w, h in ref_sizes) * KLEIN_9B_INPUT_MP
    else:
        raise InvalidRequest(f"No known neuron price for model {model}; refusing to use it.")
    return round(out + inp, 2)


def detect_format(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


class CloudflareWorkersAI:
    name = "cloudflare"
    max_references = 4
    reference_max_side = 512

    def __init__(
        self,
        account_id: Secret,
        api_token: Secret,
        model: str,
        timeout_s: float = 180,
        max_retries: int = 3,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not account_id or not api_token:
            raise AuthError("Cloudflare credentials are missing (CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN).")
        estimate_neurons(model, 512, 512, [])  # refuses models without a known price
        self._account = account_id
        self._token = api_token
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._http = session or requests.Session()
        self._sleep = sleep
        self.last_usage_error: str | None = None

    # ------------------------------------------------------------------ helpers
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.reveal()}"}

    def _url(self, path: str) -> str:
        return f"{API_BASE}/accounts/{self._account.reveal()}{path}"

    def _clean(self, text: str) -> str:
        account = self._account.reveal()
        if account:
            text = text.replace(account, mask_account_id(account))
        return redact(text, self._token)

    def _raise_for_body(self, status: int, body: dict | None, fallback_text: str) -> None:
        errors = (body or {}).get("errors") or []
        code = None
        message = fallback_text[:300]
        if errors:
            first = errors[0]
            code = first.get("code") if isinstance(first, dict) else None
            message = first.get("message", message) if isinstance(first, dict) else str(first)
        message = self._clean(f"HTTP {status}" + (f" code {code}" if code else "") + f": {message}")
        if code in QUOTA_CODES or (status == 429 and "allocation" in message.lower()):
            raise QuotaExhausted(message, code, status)
        if code in PLAN_CODES:
            raise PlanRequired(message, code, status)
        if code in TRANSIENT_CODES or status in (408, 429) or status >= 500:
            raise TransientError(message, code, status)
        if code in AUTH_CODES or status in (401, 403):
            raise AuthError(message, code, status)
        if code in INVALID_CODES or status in (400, 404, 405, 413, 422):
            raise InvalidRequest(message, code, status)
        raise ProviderError(message, code, status)

    # -------------------------------------------------------------- generation
    def estimate_cost(self, request: GenerationRequest) -> float:
        return estimate_neurons(self.model, request.width, request.height,
                                [(r.width, r.height) for r in request.references])

    def _validate(self, request: GenerationRequest) -> None:
        if not request.prompt.strip():
            raise InvalidRequest("prompt is empty")
        if len(request.references) > self.max_references:
            raise InvalidRequest(f"{len(request.references)} references; the model accepts at most {self.max_references}")
        for side in (request.width, request.height):
            if not 256 <= side <= 1920:
                raise InvalidRequest(f"output size {request.width}x{request.height} outside 256-1920")
        for ref in request.references:
            if ref.width > self.reference_max_side or ref.height > self.reference_max_side:
                raise InvalidRequest(f"reference {ref.ref_id} is {ref.width}x{ref.height}; must fit within 512x512")

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self._validate(request)
        fields: dict[str, tuple] = {
            "prompt": (None, request.prompt),
            "width": (None, str(request.width)),
            "height": (None, str(request.height)),
        }
        if request.seed is not None:
            fields["seed"] = (None, str(request.seed))
        if request.guidance is not None:
            fields["guidance"] = (None, str(request.guidance))
        for i, ref in enumerate(request.references):
            fields[f"input_image_{i}"] = (f"{ref.ref_id}.png", ref.png_bytes, "image/png")

        url = self._url(f"/ai/run/{self.model}")
        attempt = 0
        # Attempts whose outcome is unknown (timeout / dropped connection) may still
        # have been billed by the provider; callers charge them to the ledger.
        uncertain = 0
        while True:
            attempt += 1
            try:
                result = self._generate_once(url, fields, request, attempt)
                result.meta["uncertain_attempts"] = uncertain
                return result
            except requests.Timeout as exc:
                uncertain += 1
                error: ProviderError = TransientError(self._clean(f"request timed out: {exc}"))
            except requests.ConnectionError as exc:
                uncertain += 1
                error = TransientError(self._clean(f"connection error: {exc}"))
            except ProviderError as exc:
                exc.uncertain_attempts = uncertain
                if not exc.retryable:
                    raise
                error = exc
            error.uncertain_attempts = uncertain
            if attempt > self.max_retries:
                raise error
            self._sleep(min(60.0, 5.0 * (3 ** (attempt - 1))))

    def _generate_once(self, url: str, fields: dict, request: GenerationRequest, attempt: int) -> GenerationResult:
        resp = self._http.post(url, headers=self._headers(), files=fields, timeout=self.timeout_s)
        content_type = resp.headers.get("content-type", "")
        if resp.ok and content_type.startswith("image/"):
            data = resp.content
        else:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if not resp.ok or not (body or {}).get("success", False):
                self._raise_for_body(resp.status_code, body, resp.text)
            image_b64 = ((body or {}).get("result") or {}).get("image")
            if not image_b64:
                raise ProviderError("Response contained no image (result.image missing).", None, resp.status_code)
            try:
                data = base64.b64decode(image_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ProviderError(f"result.image is not valid base64: {exc}", None, resp.status_code) from exc
        fmt = detect_format(data)
        if fmt is None:
            raise ProviderError("Provider returned bytes that are not a PNG/JPEG/WebP image.", None, resp.status_code)
        return GenerationResult(
            image_bytes=data,
            image_format=fmt,
            provider=self.name,
            model=self.model,
            estimated_cost=self.estimate_cost(request),
            attempts=attempt,
            meta={"cf_ray": resp.headers.get("cf-ray", "")},
        )

    # ------------------------------------------------------------------ checks
    def _get(self, url: str) -> requests.Response:
        return self._http.get(url, headers=self._headers(), timeout=30)

    def verify_token(self) -> CheckResult:
        """Account-owned tokens verify under /accounts/{id}; user tokens under /user."""
        last = ""
        for url in (self._url("/tokens/verify"), f"{API_BASE}/user/tokens/verify"):
            try:
                resp = self._get(url)
                body = resp.json()
            except (requests.RequestException, ValueError) as exc:
                last = self._clean(str(exc))
                continue
            if resp.ok and body.get("success"):
                status = (body.get("result") or {}).get("status", "unknown")
                expires = (body.get("result") or {}).get("expires_on") or "no expiry"
                return CheckResult("API token", status == "active", f"status={status}, expires={expires}")
            errs = body.get("errors") or [{}]
            last = self._clean(f"HTTP {resp.status_code}: {errs[0].get('message', '')}")
        return CheckResult("API token", False, f"token could not be verified ({last})")

    def check_model(self) -> CheckResult:
        try:
            resp = self._get(self._url(f"/ai/models/search?search={self.model.rsplit('/', 1)[-1]}"))
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            return CheckResult("Model availability", None, self._clean(f"could not query model catalogue: {exc}"))
        if resp.status_code in (401, 403):
            return CheckResult("Model availability", False,
                               "token lacks Workers AI permission (use the 'Workers AI' token template)")
        if not resp.ok or not body.get("success"):
            errs = body.get("errors") or [{}]
            return CheckResult("Model availability", None, self._clean(f"HTTP {resp.status_code}: {errs[0].get('message', '')}"))
        names = {m.get("name") for m in body.get("result") or []}
        if self.model in names:
            return CheckResult("Model availability", True, f"{self.model} is listed for this account")
        return CheckResult("Model availability", False, f"{self.model} not in the account's model catalogue")

    def _graphql(self, query: str, variables: dict) -> dict:
        resp = self._http.post(
            f"{API_BASE}/graphql", headers=self._headers(),
            json={"query": query, "variables": variables}, timeout=30,
        )
        body = resp.json()
        if body.get("errors"):
            raise ProviderError(self._clean(str(body["errors"][0].get("message", body["errors"][0]))))
        return body.get("data") or {}

    def usage_today(self) -> float | None:
        """Neurons used today (UTC) according to Cloudflare analytics, or None if unavailable.

        Needs the optional 'Account Analytics: Read' permission. The neuron field
        name is discovered by introspection rather than assumed. On failure the
        reason is kept in ``last_usage_error`` for the health check.
        """
        self.last_usage_error = None
        try:
            schema = self._graphql(
                'query { __type(name: "AccountAiInferenceAdaptiveGroupsSum") { fields { name } } }', {}
            )
            fields = [f["name"] for f in ((schema.get("__type") or {}).get("fields") or [])]
            neuron_fields = [f for f in fields if "neuron" in f.lower()]
            if not neuron_fields:
                self.last_usage_error = "no neuron field in the analytics schema"
                return None
            field_name = neuron_fields[0]
            start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            data = self._graphql(
                "query($acct: string!, $start: Time!, $end: Time!) { viewer { accounts(filter: {accountTag: $acct}) {"
                " aiInferenceAdaptiveGroups(limit: 10000, filter: {datetime_geq: $start, datetime_lt: $end})"
                f" {{ sum {{ {field_name} }} }} }} }} }}",
                {"acct": self._account.reveal(), "start": start.isoformat().replace("+00:00", "Z"),
                 "end": end.isoformat().replace("+00:00", "Z")},
            )
            groups = data["viewer"]["accounts"][0]["aiInferenceAdaptiveGroups"]
            return float(sum((g.get("sum") or {}).get(field_name) or 0 for g in groups))
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, ProviderError) as exc:
            self.last_usage_error = self._clean(str(exc)) or type(exc).__name__
            return None

    def checks(self) -> list[CheckResult]:
        results = [self.verify_token(), self.check_model()]
        used = self.usage_today()
        if used is None:
            reason = self.last_usage_error or "unknown"
            hint = " — add 'Account > Account Analytics > Read' to the token" if "not authorized" in reason else ""
            results.append(CheckResult(
                "Cloudflare-reported usage today", None,
                f"unavailable ({reason}{hint}); the local ledger cap is still enforced",
            ))
        else:
            results.append(CheckResult("Cloudflare-reported usage today", True, f"{used:.1f} neurons used since 00:00 UTC"))
        return results
