"""Environment loading: ``.env`` in, credentials resolved, nothing logged.

The rule this module exists to enforce: **a secret is read in exactly one place**. If a
key has to be looked up in three modules with three different variable names, one of them
will be misspelt and the bot will silently fall back to paper — the most expensive kind
of bug this project can have, because everything *looks* fine.

Variable names
--------------
The canonical names are the ones the config already uses (``execution.api_key_env`` and
``execution.api_secret_env`` in ``config/settings.json``). For convenience the loader also
accepts the common ``CCXT_*`` aliases, so a ``.env`` written from a generic exchange
tutorial still works. Aliases are *read-only*: nothing is ever written back.

What is deliberately **not** supported
-------------------------------------
MetaTrader 5. It is Windows-only, it is not a venue this project can reach from the
sandbox or a Linux VPS, and there is no code path that would use an ``MT5_*`` credential.
Accepting those variables would create the impression of a second execution venue that
does not exist, so they are flagged as *ignored* instead of quietly accepted.

Nothing here ever prints or returns a secret value. Only :func:`credential_status` leaves
this module, and it reports presence, length class and source — never the secret.
"""

from __future__ import annotations

import functools
import hashlib
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

#: canonical name -> accepted aliases (the canonical name always wins)
ALIASES: dict[str, tuple[str, ...]] = {
    "TO_CONFIG": (),
    "TO_API_KEY": ("CCXT_API_KEY",),
    "TO_API_SECRET": ("CCXT_SECRET_KEY", "CCXT_API_SECRET"),
    "TO_EXCHANGE_ID": ("CCXT_EXCHANGE_ID",),
    "TO_TESTNET": ("CCXT_TESTNET",),
    "TO_STATE_ENCRYPTION_KEY": ("STATE_ENCRYPTION_KEY",),
    "TO_STATE_PASSPHRASE": ("STATE_PASSPHRASE",),
    # a bot token pasted from a generic tutorial is usually named after the service
    "TO_TELEGRAM_TOKEN": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "TG_TOKEN"),
    # Exness Trader API (docs: exness-api.com). The public key and the account id are not
    # secrets; the Ed25519 private key is, so it can also live in a file outside the repo.
    "TO_EXNESS_API_KEY": ("EXNESS_API_KEY",),
    "TO_EXNESS_PRIVATE_KEY": ("EXNESS_PRIVATE_KEY",),
    "TO_EXNESS_PRIVATE_KEY_PATH": ("EXNESS_PRIVATE_KEY_PATH", "TO_EXNESS_PRIVATE_KEY_FILE"),
    "TO_EXNESS_ACCOUNT_ID": ("EXNESS_ACCOUNT_ID", "TO_EXNESS_ACCT"),
    "TO_EXNESS_GATEWAY_HOST": ("EXNESS_GATEWAY_HOST", "EXN_GATEWAY_HOST"),
    # Gotrade (US shares). A single bearer token from Profile > API Key, valid one year.
    "TO_GOTRADE_TOKEN": ("GOTRADE_TOKEN", "GOTRADE_API_KEY", "HEYGOTRADE_TOKEN"),
}

#: variables from other ecosystems that this project cannot honour (see the docstring)
UNSUPPORTED_PREFIXES: tuple[str, ...] = ("MT5_", "METAQUOTES_")


@functools.lru_cache(maxsize=8)
def _load(path: Path) -> tuple[tuple[str, str], ...]:
    """Load one env file. Real environment variables always win over the file."""
    pairs: dict[str, str] = {}
    try:
        from dotenv import dotenv_values  # type: ignore

        values = dotenv_values(path)
        for key, value in values.items():
            if value is not None:
                pairs[key] = value
    except Exception:  # python-dotenv absent or the file is unreadable
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip().removeprefix("export ").strip()
                pairs[key] = value.strip().strip("'\"")
        except OSError:
            return ()
    for key, value in pairs.items():
        os.environ.setdefault(key, value)  # the real environment wins, never overwrite
    return tuple(sorted(pairs.items()))


def load_env(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load ``.env`` (idempotent) and report what happened — without the values."""
    target = Path(path) if path else Path(os.environ.get("TO_ENV_FILE") or DEFAULT_ENV_FILE)
    if not target.exists():
        return {"file": str(target), "found": False, "loaded": 0, "keys": [],
                "unsupported": [], "library": _library()}
    pairs = _load(target)
    keys = [k for k, _ in pairs]
    return {
        "file": str(target),
        "found": True,
        "loaded": len(pairs),
        "keys": keys,
        "unsupported": [k for k in keys if k.startswith(UNSUPPORTED_PREFIXES)],
        "library": _library(),
    }


def _library() -> str:
    try:
        import dotenv  # noqa: F401

        return "python-dotenv"
    except Exception:
        return "built-in parser"


def get(name: str, default: str = "") -> str:
    """Canonical name, then its aliases, then the default."""
    for candidate in (name, *ALIASES.get(name, ())):
        value = os.environ.get(candidate)
        if value not in (None, ""):
            return str(value)
    return default


def resolved_name(name: str) -> str | None:
    """Which variable actually supplied the value (for diagnostics)."""
    for candidate in (name, *ALIASES.get(name, ())):
        if os.environ.get(candidate) not in (None, ""):
            return candidate
    return None


def credential(name: str) -> str:
    """Resolve a credential named by config (``execution.api_key_env``).

    The configured name may be a canonical name, an alias, or a comma-separated list of
    candidates: ``"TO_API_KEY,CCXT_API_KEY"`` resolves the first one that is set.
    """
    for candidate in str(name).split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        value = get(candidate)
        if value:
            return value
    return ""


def fingerprint(value: str) -> str:
    """Stable, non-reversible tag so a log can say *which* key is loaded."""
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def credential_status(*names: str) -> dict[str, Any]:
    """Presence report for the UI/diagnostics. Never contains a secret."""
    out: dict[str, Any] = {}
    for name in names:
        value = credential(name)
        out[name] = {
            "set": bool(value),
            "via": resolved_name(name),
            "length": len(value),
            "fingerprint": fingerprint(value),
        }
    return out
