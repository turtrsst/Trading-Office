"""Optional at-rest encryption for the state directory.

What gets encrypted
-------------------
``signals.json`` (bar locks), ``positions.json`` (open positions), ``cooldowns.json``,
``snapshot*.json`` and ``ledger.jsonl``. Those are the files that describe what the bot
owns and what it did — the ones worth stealing and, more importantly, the ones whose
corruption makes the bot trade twice.

How it is enabled
-----------------
Both of these are off by default and both are read only from the environment:

``TO_STATE_ENCRYPTION_KEY``
    A Fernet key (``Fernet.generate_key()``). Recommended.
``TO_STATE_PASSPHRASE``
    A passphrase, stretched with scrypt into a Fernet key. Convenient for a VPS where you
    do not want to paste a base64 blob into a service file.

Format
------
The ciphertext is written **raw** (that is what Fernet produces), so a state file either
parses as JSON or does not. Detection is by content, not by extension: an existing
plaintext state directory keeps working when encryption is switched on, and files are
re-encrypted as they are rewritten. That matters — a bot that refuses to start because it
cannot read yesterday's ``positions.json`` is worse than one running unencrypted.

Failure policy
--------------
A wrong key is **fatal and loud**. The tempting alternative — catch the error, return an
empty state — would make the engine believe it holds no positions, re-enter symbols it
already owns, and re-fire entries whose bar lock it "forgot". Every caller of
:func:`decrypt_bytes` must therefore let :class:`StateLocked` propagate.
"""

from __future__ import annotations

import json
import os
from typing import Any

FERNET_PREFIX = b"gAAAAA"  # first bytes of every Fernet token (version 0x80, base64url)


class StateLocked(RuntimeError):
    """The state could not be decrypted: wrong key, or a corrupt file.

    Deliberately an exception: callers must not continue with an empty state.
    """


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def encryption_requested() -> bool:
    return bool(_env("TO_STATE_ENCRYPTION_KEY", "STATE_ENCRYPTION_KEY",
                     "TO_STATE_PASSPHRASE", "STATE_PASSPHRASE"))


def _key() -> bytes | None:
    """Derive the Fernet key, cached per process."""
    cached = getattr(_key, "_cache", None)
    if cached is not None:
        return cached or None

    from cryptography.fernet import Fernet  # imported lazily: optional dependency

    raw = _env("TO_STATE_ENCRYPTION_KEY", "STATE_ENCRYPTION_KEY")
    key: bytes
    if raw:
        key = raw.encode("utf-8")
        try:
            Fernet(key)  # validates length/format early, with a clear error
        except Exception as exc:  # noqa: BLE001
            raise StateLocked(
                "TO_STATE_ENCRYPTION_KEY is not a valid Fernet key "
                "(generate one with: python -c \"from cryptography.fernet import "
                "Fernet;print(Fernet.generate_key().decode())\")"
            ) from exc
    else:
        passphrase = _env("TO_STATE_PASSPHRASE", "STATE_PASSPHRASE")
        if not passphrase:
            _key._cache = b""  # type: ignore[attr-defined]
            return None
        import base64
        import hashlib

        derived = hashlib.scrypt(passphrase.encode("utf-8"), salt=b"trading-office/state",
                                 n=2 ** 14, r=8, p=1, dklen=32)
        key = base64.urlsafe_b64encode(derived)
    _key._cache = key  # type: ignore[attr-defined]
    return key


def _fernet():
    key = _key()
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key)


# --------------------------------------------------------------------------- bytes
def encrypt_bytes(payload: bytes) -> bytes:
    """Encrypt if a key is configured; otherwise pass the bytes through untouched."""
    cipher = _fernet()
    return payload if cipher is None else cipher.encrypt(payload)


def looks_encrypted(payload: bytes) -> bool:
    head = payload.lstrip()[:8]
    return head.startswith(FERNET_PREFIX) and not head.startswith((b"{", b"["))


def decrypt_bytes(payload: bytes) -> bytes:
    """Decrypt a state file. Plaintext passes through so old directories keep working."""
    if not payload or not looks_encrypted(payload):
        return payload
    cipher = _fernet()
    if cipher is None:
        raise StateLocked(
            "this state file is encrypted but no TO_STATE_ENCRYPTION_KEY / "
            "TO_STATE_PASSPHRASE is set"
        )
    try:
        return cipher.decrypt(payload)
    except Exception as exc:  # noqa: BLE001 - InvalidToken and friends
        raise StateLocked(
            "cannot decrypt the state files: the key does not match the one that wrote "
            "them (refusing to start with an empty book)"
        ) from exc


# --------------------------------------------------------------------------- json
def dump_json(payload: Any) -> bytes:
    raw = json.dumps(payload, indent=2, ensure_ascii=False, default=str).encode("utf-8")
    return encrypt_bytes(raw)


def load_json(payload: bytes, default: Any = None) -> Any:
    if not payload:
        return default
    try:
        return json.loads(decrypt_bytes(payload).decode("utf-8"))
    except json.JSONDecodeError:
        return default


def status() -> dict[str, Any]:
    """Report the encryption mode without leaking the key."""
    if not encryption_requested():
        return {"encrypted": False, "source": None, "algorithm": None}
    try:
        _key()
    except StateLocked as exc:
        return {"encrypted": False, "source": "invalid", "algorithm": None, "error": str(exc)}
    source = "TO_STATE_ENCRYPTION_KEY" if _env("TO_STATE_ENCRYPTION_KEY", "STATE_ENCRYPTION_KEY") else "TO_STATE_PASSPHRASE"
    return {"encrypted": True, "source": source, "algorithm": "Fernet (AES-128-CBC + HMAC-SHA256)"}
