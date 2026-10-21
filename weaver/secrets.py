"""Credential loading that never exposes secret values.

Lookup order: process environment (e.g. Claude Code cloud environment secrets),
then the git-ignored ``.env`` file. Values are wrapped in :class:`Secret` so an
accidental ``print``/``repr``/log line shows ``****`` instead of the token.
"""

from __future__ import annotations

import getpass
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from weaver.config import ENV_FILE, ROOT

ACCOUNT_ID = "CLOUDFLARE_ACCOUNT_ID"
API_TOKEN = "CLOUDFLARE_API_TOKEN"


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('****')"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True)
class Credential:
    value: Secret | None
    source: str  # "environment", ".env" or "missing"


def get_credential(name: str, env_file: Path = ENV_FILE) -> Credential:
    if os.environ.get(name):
        return Credential(Secret(os.environ[name].strip()), "environment")
    file_values = parse_env_file(env_file)
    if file_values.get(name):
        return Credential(Secret(file_values[name]), ".env")
    return Credential(None, "missing")


def mask_account_id(account_id: str) -> str:
    return f"…{account_id[-4:]}" if len(account_id) > 4 else "****"


def redact(text: str, *secrets: Secret | None) -> str:
    """Remove secret values (and the account id) from text before showing or logging it."""
    for secret in secrets:
        if secret:
            value = secret.reveal()
            if value:
                text = text.replace(value, "****")
    return text


def env_file_is_ignored(env_file: Path = ENV_FILE) -> bool:
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(env_file)],
            cwd=ROOT, capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def interactive_setup(env_file: Path = ENV_FILE) -> str:
    """Prompt for credentials with hidden input and write them to .env (mode 0600)."""
    if not env_file_is_ignored(env_file):
        raise RuntimeError(".env is not git-ignored; refusing to write credentials.")
    account_id = input("Cloudflare Account ID (32 hex characters): ").strip()
    token = getpass.getpass("Cloudflare API token (input hidden): ").strip()
    if not account_id or not token:
        raise RuntimeError("Both values are required; nothing was written.")
    existing = parse_env_file(env_file)
    existing[ACCOUNT_ID] = account_id
    existing[API_TOKEN] = token
    body = "".join(f"{k}={v}\n" for k, v in existing.items())
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# Written by `python -m weaver setup`. Git-ignored. Never commit.\n")
        fh.write(body)
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass
    return str(env_file)
