# encrypt.py
# Encrypt cookies.json into cookies.json.encrypted using DECRYPT_KEY from .env
# After successful encryption, removes the plaintext cookies.json

import os
import json
import base64
from secrets import token_bytes
from dotenv import load_dotenv
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT_DIR = os.path.dirname(__file__)
PLAIN_COOKIE_FILE = os.path.join(ROOT_DIR, "cookies.json")
# Write encrypted output to the correct filename
ENCRYPTED_COOKIE_FILE = os.path.join(ROOT_DIR, "cookies.json.encrypted")


def _derive_key_from_password(password: bytes, salt: bytes) -> bytes:
    # PBKDF2-HMAC-SHA256 to derive a 256-bit key
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=200_000,
    )
    return kdf.derive(password)


def _encrypt_bytes(data: bytes, password: str) -> dict:
    salt = token_bytes(16)
    nonce = token_bytes(12)
    key = _derive_key_from_password(password.encode("utf-8"), salt)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, data, None)
    return {
        "v": 1,
        "s": base64.b64encode(salt).decode("ascii"),
        "n": base64.b64encode(nonce).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
    }


def main() -> None:
    load_dotenv()
    key = os.getenv("DECRYPT_KEY")
    if not key:
        raise RuntimeError("DECRYPT_KEY is missing in environment/.env")

    if not os.path.exists(PLAIN_COOKIE_FILE):
        raise FileNotFoundError("cookies.json not found to encrypt")

    with open(PLAIN_COOKIE_FILE, "rb") as f:
        plaintext = f.read()

    payload = _encrypt_bytes(plaintext, key)

    # Write encrypted payload as JSON text
    with open(ENCRYPTED_COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    # Remove plaintext cookies
    try:
        os.remove(PLAIN_COOKIE_FILE)
    except Exception:
        pass


if __name__ == "__main__":
    main()