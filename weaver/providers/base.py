"""Provider-independent contract for image generation.

The rest of the pipeline only talks to :class:`ImageProvider`. Swapping
Cloudflare for another backend (Hugging Face, a Kaggle notebook, a future
model) means adding one module here; nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ReferenceInput:
    ref_id: str
    png_bytes: bytes
    width: int
    height: int
    sha256: str


@dataclass(frozen=True)
class GenerationRequest:
    prompt: str
    width: int
    height: int
    references: tuple[ReferenceInput, ...] = ()
    seed: int | None = None
    guidance: float | None = None


@dataclass(frozen=True)
class GenerationResult:
    image_bytes: bytes
    image_format: str  # "png", "jpeg" or "webp"
    provider: str
    model: str
    estimated_cost: float
    attempts: int
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool | None  # None = could not be determined
    detail: str


class ProviderError(Exception):
    """A provider call failed. ``code`` is the provider's own error code if any."""

    retryable = False

    def __init__(self, message: str, code: int | None = None, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.uncertain_attempts = 0


class TransientError(ProviderError):
    retryable = True


class QuotaExhausted(ProviderError):
    """Daily free allocation used up. Never retried; resume after the reset."""


class AuthError(ProviderError):
    """Missing/invalid credentials or insufficient token permissions."""


class PlanRequired(ProviderError):
    """The model needs a paid plan. The pipeline never upgrades a plan."""


class InvalidRequest(ProviderError):
    """The request itself was rejected (bad size, too-large reference, bad model id)."""


class ImageProvider(Protocol):
    name: str
    model: str
    max_references: int
    reference_max_side: int

    def estimate_cost(self, request: GenerationRequest) -> float: ...

    def generate(self, request: GenerationRequest) -> GenerationResult: ...

    def checks(self) -> list[CheckResult]: ...

    def usage_today(self) -> float | None: ...
