"""Local storage for THSR booking app.

- profiles.json    plain-text trip / passenger profiles (no card data)
- cards.enc        Fernet-encrypted credit-card vault (key derived from
                   user master password via PBKDF2)
- cards.salt       per-vault random salt
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Optional


CONFIG_DIR = Path(os.environ.get("THSR_DATA_DIR") or os.path.expanduser("~/.thsr"))
PROFILES_PATH = CONFIG_DIR / "profiles.json"
CARDS_PATH = CONFIG_DIR / "cards.enc"
CARD_SALT_PATH = CONFIG_DIR / "cards.salt"

PROFILE_FIELDS = (
    "start", "dest", "date", "time", "time_from", "time_until",
    "adults", "id_number", "phone", "email",
    "retry", "retry_interval", "retry_max",
)


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------- profiles (plain) ----------

def load_profiles() -> dict:
    if not PROFILES_PATH.exists():
        return {"active": None, "profiles": {}}
    try:
        data = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
        data.setdefault("profiles", {})
        data.setdefault("active", None)
        return data
    except (OSError, json.JSONDecodeError):
        return {"active": None, "profiles": {}}


def save_profiles(data: dict) -> None:
    _ensure_dir()
    PROFILES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _restrict(PROFILES_PATH)


def upsert_profile(name: str, fields: dict, set_active: bool = True) -> None:
    data = load_profiles()
    cleaned = {k: fields.get(k) for k in PROFILE_FIELDS if k in fields}
    data["profiles"][name] = cleaned
    if set_active:
        data["active"] = name
    save_profiles(data)


def delete_profile(name: str) -> None:
    data = load_profiles()
    data["profiles"].pop(name, None)
    if data.get("active") == name:
        data["active"] = None
    save_profiles(data)


def get_profile(name: str) -> Optional[dict]:
    return load_profiles()["profiles"].get(name)


def list_profiles() -> list[str]:
    return sorted(load_profiles()["profiles"].keys())


def active_profile_name() -> Optional[str]:
    return load_profiles().get("active")


# ---------- card vault (encrypted) ----------

def vault_exists() -> bool:
    return CARDS_PATH.exists() and CARD_SALT_PATH.exists()


def _derive_fernet(password: str, salt: bytes):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
    return Fernet(key)


class WrongPassword(Exception):
    pass


class CardVault:
    """Encrypted credit-card vault.

    Cards are list[dict] with keys: alias, holder, number, expiry, cvc, note.
    The CVC is optional (some users prefer to not store it).
    """

    def __init__(self, password: str):
        self._password = password
        self._cards: list[dict] = []
        self._loaded = False

    def load(self) -> list[dict]:
        if not vault_exists():
            self._cards = []
            self._loaded = True
            return self._cards
        try:
            from cryptography.fernet import InvalidToken
        except ImportError as e:
            raise RuntimeError(
                "cryptography 套件未安裝；請執行 pip install cryptography"
            ) from e
        salt = CARD_SALT_PATH.read_bytes()
        f = _derive_fernet(self._password, salt)
        try:
            plain = f.decrypt(CARDS_PATH.read_bytes())
        except InvalidToken as e:
            raise WrongPassword("主密碼錯誤") from e
        payload = json.loads(plain.decode("utf-8"))
        self._cards = list(payload.get("cards", []))
        self._loaded = True
        return self._cards

    def save(self, cards: list[dict]) -> None:
        _ensure_dir()
        if CARD_SALT_PATH.exists():
            salt = CARD_SALT_PATH.read_bytes()
        else:
            salt = os.urandom(16)
            CARD_SALT_PATH.write_bytes(salt)
            _restrict(CARD_SALT_PATH)
        f = _derive_fernet(self._password, salt)
        token = f.encrypt(
            json.dumps({"cards": cards}, ensure_ascii=False).encode("utf-8")
        )
        CARDS_PATH.write_bytes(token)
        _restrict(CARDS_PATH)
        self._cards = cards
        self._loaded = True

    @property
    def cards(self) -> list[dict]:
        if not self._loaded:
            self.load()
        return self._cards


def mask_card_number(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) <= 4:
        return digits
    return "**** **** **** " + digits[-4:]
