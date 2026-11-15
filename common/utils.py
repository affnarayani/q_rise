import base64
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, List

import requests

from common.config import get_path


def custom_random_wait(config: Dict[str, Any], mode: str = "absolute") -> None:
    if mode == "random":
        min_sec = config["min_random_wait"]
        max_sec = config["max_random_wait"]
    else:
        min_sec = config["min_absolute_wait"]
        max_sec = config["max_absolute_wait"]
    seconds = random.uniform(min_sec, max_sec)
    print(f"[WAIT] Sleeping for {seconds:.2f} seconds...", flush=True)
    time.sleep(seconds)


def _derive_key(password: bytes, salt: bytes, iterations: int) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(password)


def _decrypt_payload(payload: Dict[str, Any], password: str, iterations: int) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = base64.b64decode(payload["s"])
    nonce = base64.b64decode(payload["n"])
    ciphertext = base64.b64decode(payload["ct"])
    key = _derive_key(password.encode("utf-8"), salt, iterations)
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise RuntimeError("Decryption failed (InvalidTag)") from exc


def load_encrypted_json(file_path: Path, decrypt_key: str, iterations: int) -> Any:
    with file_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    plaintext = _decrypt_payload(payload, decrypt_key, iterations)
    return json.loads(plaintext.decode("utf-8"))


def normalize_cookie_same_site(value: Any) -> str:
    val = str(value).lower()
    if val in ["no_restriction", "none", "unspecified", "null"]:
        return "None"
    if val == "lax":
        return "Lax"
    if val == "strict":
        return "Strict"
    return "Lax"


def load_playwright_cookies(file_path: Path, decrypt_key: str, iterations: int) -> List[Dict[str, Any]]:
    cookies = load_encrypted_json(file_path, decrypt_key, iterations)
    formatted = []
    for c in cookies:
        cookie = {
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
        }
        if "expirationDate" in c:
            cookie["expires"] = float(int(c["expirationDate"]))
        elif "expiry" in c:
            cookie["expires"] = float(int(c["expiry"]))
        elif "expires" in c:
            try:
                cookie["expires"] = float(c["expires"])
            except Exception:
                pass
        if "sameSite" in c:
            cookie["sameSite"] = normalize_cookie_same_site(c["sameSite"])
        formatted.append(cookie)
    return formatted


def pick_random_encrypted_cookie(cookies_dir: Path) -> Path:
    encrypted_files = list(cookies_dir.glob("*.encrypted"))
    if not encrypted_files:
        raise RuntimeError(f"No .encrypted cookie files found in '{cookies_dir.as_posix()}' folder")
    selected = random.choice(encrypted_files)
    print(f"[OK] Randomly selected cookie file: {selected.name}", flush=True)
    return selected


def pick_first_encrypted_cookie(cookies_dir: Path) -> Path:
    encrypted_files = sorted(cookies_dir.glob("*.encrypted"))
    if not encrypted_files:
        raise RuntimeError(f"No .encrypted cookie files found in '{cookies_dir.as_posix()}' folder")
    return encrypted_files[0]


def ensure_error_dir(config: Dict[str, Any]) -> Path:
    error_dir = get_path(config, "error_dir")
    error_dir.mkdir(parents=True, exist_ok=True)
    return error_dir


def get_error_screenshot_path(config: Dict[str, Any]) -> Path:
    ensure_error_dir(config)
    return get_path(config, "error_screenshot_file")


def upload_to_onlyfiles(screenshot_path: Path, config: Dict[str, Any]) -> str:
    if not screenshot_path.exists():
        print(f"[WARNING] Screenshot file not found: {screenshot_path}", flush=True)
        return ""
    url = config["onlyfiles"]["upload_url"]
    expire = config["onlyfiles"]["expire_seconds"]
    try:
        with screenshot_path.open("rb") as file:
            response = requests.post(url, files={"file": file}, data={"expire": expire}, timeout=60)
        if response.status_code == 200:
            res_data = response.json()
            direct_url = res_data["data"]["file"]["url"]["full"]
            print(f"👉 DIRECT LINK (Expires in {expire} seconds): {direct_url}")
            return direct_url
        print(f"[WARNING] Upload Failed: {response.status_code}")
    except Exception as e:
        print(f"[WARNING] Upload error: {e}", flush=True)
    return ""


def save_error_screenshot(page: Any, config: Dict[str, Any]) -> Path:
    screenshot_path = get_error_screenshot_path(config)
    if screenshot_path.exists():
        try:
            screenshot_path.unlink()
        except Exception:
            pass
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"[OK] Error screenshot captured: {screenshot_path.as_posix()}", flush=True)
        upload_to_onlyfiles(screenshot_path, config)
    except Exception as screenshot_err:
        print(f"[WARNING] Could not capture or upload screenshot: {screenshot_err}", flush=True)
    return screenshot_path


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def try_solve_turnstile(page: Any, timeout_sec: int = 30) -> bool:
    """Best-effort Turnstile handling for Playwright pages."""
    print("[STEP] Checking for Cloudflare Turnstile barriers...", flush=True)
    end_time = time.time() + timeout_sec
    while time.time() < end_time:
        try:
            for frame in page.frames:
                url = (frame.url or "").lower()
                if "turnstile" in url or "challenges.cloudflare.com" in url:
                    try:
                        checkbox = frame.locator("input[type='checkbox'], #challenge-stage, .ctp-checkbox-label, body").first
                        checkbox.click(timeout=1500)
                        print("[INFO] Attempted Turnstile interaction in frame.", flush=True)
                        time.sleep(2)
                    except Exception:
                        pass
            # generic checkbox on main page if present
            try:
                cb = page.locator("iframe[src*='turnstile']").first
                if cb.count() > 0:
                    box = cb.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        time.sleep(2)
            except Exception:
                pass
            title = ""
            try:
                title = (page.title() or "").lower()
            except Exception:
                pass
            if "just a moment" not in title and "attention required" not in title:
                return True
        except Exception:
            pass
        time.sleep(2)
    print("[INFO] Turnstile check completed (best effort).", flush=True)
    return False
