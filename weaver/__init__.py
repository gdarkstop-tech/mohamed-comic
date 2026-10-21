"""WEAVER comic production pipeline.

Provider-independent image generation (Cloudflare Workers AI first), resumable
panel production under a hard free-tier quota cap, page composition with English
lettering, and PDF export. Operated entirely through ``python -m weaver``.
"""

__version__ = "0.1.0"
