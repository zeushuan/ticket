"""簡單帳密保護：PBKDF2-SHA256 hash 儲存在 ~/.thsr/auth.json。

也支援環境變數 THSR_AUTH_USER / THSR_AUTH_PASS 直接設定（不寫檔），
適合在 docker-compose.yml 帶入。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from thsr_storage import CONFIG_DIR, _ensure_dir, _restrict

AUTH_PATH = CONFIG_DIR / "auth.json"
ITERATIONS = 390_000
SALT_BYTES = 16


def auth_exists() -> bool:
    return AUTH_PATH.exists()


def env_credentials() -> Optional[tuple[str, str]]:
    u = os.environ.get("THSR_AUTH_USER")
    p = os.environ.get("THSR_AUTH_PASS")
    if u and p:
        return u, p
    return None


def _pbkdf2(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )


def save_auth(user: str, password: str) -> None:
    _ensure_dir()
    salt = secrets.token_bytes(SALT_BYTES)
    h = _pbkdf2(password, salt)
    AUTH_PATH.write_text(json.dumps({
        "user": user,
        "salt": salt.hex(),
        "hash": h.hex(),
        "iterations": ITERATIONS,
    }), encoding="utf-8")
    _restrict(AUTH_PATH)


def verify(user: str, password: str) -> bool:
    """File-backed verify; returns False if no auth file."""
    if not auth_exists():
        return False
    try:
        data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    salt = bytes.fromhex(data.get("salt", ""))
    iterations = int(data.get("iterations", ITERATIONS))
    expected = bytes.fromhex(data.get("hash", ""))
    if not salt or not expected:
        return False

    actual = _pbkdf2(password, salt, iterations)
    user_ok = hmac.compare_digest(
        (data.get("user") or "").encode(), user.encode()
    )
    hash_ok = hmac.compare_digest(expected, actual)
    return user_ok and hash_ok


def authenticate(user: str, password: str) -> bool:
    """Verify against env vars first, else against the auth file.

    Sleeps briefly on failure to slow brute-force.
    """
    env = env_credentials()
    if env is not None:
        ok = (
            hmac.compare_digest(env[0].encode(), user.encode())
            and hmac.compare_digest(env[1].encode(), password.encode())
        )
    else:
        ok = verify(user, password)

    if not ok:
        time.sleep(0.5)
    return ok
