"""Encrypt/decrypt client OAuth token files at rest."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
TOKENS_DIR = ROOT / "tokens"


def _fernet() -> Fernet:
    raw = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()
    if not raw:
        # Derive a stable local key from machine+project (dev only). Prefer setting env.
        seed = (os.environ.get("GMAIL_EMAIL", "") + str(ROOT)).encode()
        key = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
        return Fernet(key)
    # Accept raw Fernet key or any passphrase
    try:
        return Fernet(raw.encode() if isinstance(raw, str) else raw)
    except Exception:
        key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
        return Fernet(key)


def token_path(client_email: str) -> Path:
    safe = (client_email or "unknown").strip().lower().replace("@", "_at_")
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in safe)
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    return TOKENS_DIR / f"{safe}.json.enc"


def save_encrypted(client_email: str, plaintext_json: str) -> Path:
    path = token_path(client_email)
    token = _fernet().encrypt(plaintext_json.encode("utf-8"))
    path.write_bytes(token)
    return path


def load_decrypted(client_email: str) -> str:
    path = token_path(client_email)
    if not path.exists():
        # also try unencrypted legacy path
        legacy = TOKENS_DIR / f"{client_email.strip().lower()}.json"
        if legacy.exists():
            return legacy.read_text(encoding="utf-8")
        raise FileNotFoundError(f"No tokens for {client_email}: {path}")
    try:
        return _fernet().decrypt(path.read_bytes()).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError(
            "Cannot decrypt token file — set TOKEN_ENCRYPTION_KEY to the same "
            "value used when the token was saved."
        ) from e
