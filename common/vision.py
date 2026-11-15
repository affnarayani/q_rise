import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from common.config import get_path
from common.locators import record_changed_locator


def _image_to_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def call_gemini_vision(config: Dict[str, Any], screenshot_path: Path, prompt: str) -> str:
    gem_cfg = config.get("gemini", {})
    model = gem_cfg.get("model", "gemini-3.5-flash-lite")
    api_key_env = gem_cfg.get("api_key_env", "GEMINI_API_KEY")
    api_key = __import__("os").getenv(api_key_env, "")
    if not api_key:
        raise RuntimeError(f"{api_key_env} missing in environment variables")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/png", "data": _image_to_base64(screenshot_path)}}]}],
        "generationConfig": {"temperature": 0.1},
    }
    resp = requests.post(url, json=payload, timeout=90)
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini vision request failed: {resp.status_code} {resp.text[:400]}")
    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini vision returned no candidates")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        raise RuntimeError("Gemini vision returned empty text")
    return text


def capture_vision_screenshot(page: Any, config: Dict[str, Any], tag: str) -> Path:
    error_dir = get_path(config, "error_dir")
    error_dir.mkdir(parents=True, exist_ok=True)
    safe_tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", tag)
    path = error_dir / f"vision_{safe_tag}_{int(time.time())}.png"
    if path.exists():
        try:
            path.unlink()
        except Exception:
            pass
    page.screenshot(path=str(path), full_page=True)
    return path


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    t = text.strip()
    m = re.search(r"```json\s*(\{.*?\})\s*```", t, re.S | re.I)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = re.search(r"(\{.*\})", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    try:
        return json.loads(t)
    except Exception:
        return None


def vision_recover(config: Dict[str, Any], platform: str, locator_name: str, failed_expressions: List[str], page: Any, intent: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    screenshot = capture_vision_screenshot(page, config, f"{platform}_{locator_name}")
    prompt = (
        "Return ONLY one JSON object with keys: action,suggested_locator,coordinates,value,reason. "
        "action must be one of: click,type,extract_text,extract_href,scroll,none. "
        "suggested_locator must be a future-use Playwright locator expression (e.g. page.get_by_role(...), page.locator(...), page.get_by_text(...)) for this platform. "
        "Do NOT suggest any locator starting with # and do NOT suggest page.locator('#...') or locator('#...') id selectors. "
        "coordinates must be {\"x\": number, \"y\": number} when pixel interaction is needed. "
        f"Platform={platform}, locator_name={locator_name}, intent={intent}. "
        f"Failed locator expressions={json.dumps(failed_expressions, ensure_ascii=False)}. "
        f"Extra={json.dumps(extra or {}, ensure_ascii=False)}. "
        "Complete current run via action/coordinates/value guidance only; suggested_locator is for next runs."
    )
    try:
        raw = call_gemini_vision(config, screenshot, prompt)
    except Exception as e:
        return {"action": "none", "suggested_locator": "", "coordinates": None, "value": "", "reason": str(e), "raw": ""}
    parsed = _extract_json_object(raw) or {}
    suggested = str(parsed.get("suggested_locator", "")).strip()
    if suggested.startswith("#") or re.search(r"page\.locator\(\s*['\"]#", suggested):
        suggested = ""
    primary_failed = failed_expressions[0] if failed_expressions else ""
    if primary_failed and suggested:
        record_changed_locator(config, primary_failed, suggested)
    parsed["suggested_locator"] = suggested
    parsed["raw"] = raw
    parsed["screenshot"] = str(screenshot)
    return parsed
