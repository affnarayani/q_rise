import os
import sys
import json
import time
import base64
import random
from pathlib import Path
from typing import List, Dict, Any

from dotenv import load_dotenv

from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


# =========================
# CONFIG
# =========================
HEADLESS = True

QUORA_COOKIES_FILE = "quora_cookies.json.encrypted"
TOPICS_FILE = "topics.txt"
STATUS_FILE = "status.json"
ANSWERED_JSON_FILE = "answered.json"
PBKDF2_ITERATIONS = 200_000

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


# =========================
# DYNAMIC WAITS
# =========================
def custom_random_wait(min_sec=15, max_sec=30):
    seconds = random.uniform(min_sec, max_sec)
    print(f"[WAIT] Sleeping for {seconds:.2f} seconds...", flush=True)
    time.sleep(seconds)


# =========================
# CRYPTO (COOKIES DECRYPTION)
# =========================
def _derive_key(password: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(password)


def _decrypt_payload(payload: Dict[str, Any], password: str) -> bytes:
    salt = base64.b64decode(payload["s"])
    nonce = base64.b64decode(payload["n"])
    ciphertext = base64.b64decode(payload["ct"])

    key = _derive_key(password.encode("utf-8"), salt)
    aesgcm = AESGCM(key)

    try:
        return aesgcm.decrypt(nonce, ciphertext, None)
    except InvalidTag:
        raise RuntimeError("❌ Decryption failed (InvalidTag)")


def load_cookies(file_path: Path, decrypt_key: str) -> List[Dict[str, Any]]:
    print("[STEP] Loading and decrypting cookies...", flush=True)

    with file_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    plaintext = _decrypt_payload(payload, decrypt_key)
    cookies = json.loads(plaintext.decode("utf-8"))

    # Cookie format cleaning
    for c in cookies:
        if "partitionKey" in c and isinstance(c["partitionKey"], dict):
            if "topLevelSite" in c["partitionKey"]:
                c["partitionKey"] = str(c["partitionKey"]["topLevelSite"])
            else:
                del c["partitionKey"]

        if "sameSite" in c:
            val = str(c["sameSite"]).lower()
            if val in ["no_restriction", "none", "unspecified", "null"]:
                c["sameSite"] = "None"
            elif val == "lax":
                c["sameSite"] = "Lax"
            elif val == "strict":
                c["sameSite"] = "Strict"
            else:
                c["sameSite"] = "Lax"

    print("[OK] Cookies loaded successfully", flush=True)
    return cookies


# =========================
# TOPIC SELECTION UTILITY
# =========================
def get_random_topic(file_path: str) -> str:
    path = Path(file_path)
    if not path.exists():
        print(f"[CRITICAL] {file_path} file nahi mili! Exiting.", flush=True)
        sys.exit(1)
        
    with open(path, "r", encoding="utf-8") as f:
        topics = [line.strip() for line in f if line.strip()]
        
    if not topics:
        print(f"[CRITICAL] {file_path} khali hai! Exiting.", flush=True)
        sys.exit(1)
        
    selected_topic = random.choice(topics)
    return selected_topic


# =========================
# LINK DUPLICATE CHECKER
# =========================
def is_link_already_answered(url: str, answered_file: str) -> bool:
    answered_path = Path(answered_file)
    if not answered_path.exists():
        return False
        
    try:
        with answered_path.open("r", encoding="utf-8") as af:
            answered_list = json.load(af)
            if not isinstance(answered_list, list):
                return False
                
            # Trailing slashes '/' remove karke normalize karna
            def normalize(link: str) -> str:
                return str(link).strip().rstrip("/")
                
            target_norm = normalize(url)
            for existing_link in answered_list:
                if normalize(existing_link) == target_norm:
                    return True
    except Exception as e:
        print(f"[WARNING] Error reading {answered_file}: {e}", flush=True)
        
    return False


# =========================
# MAIN
# =========================
def run(decrypt_key: str):
    print("[START] Script started", flush=True)

    # =========================
    # 1. STATUS JSON GATEKEEPER CHECK
    # =========================
    status_path = Path(STATUS_FILE)
    if not status_path.exists():
        print(f"[CRITICAL] {STATUS_FILE} nahi mila. Exiting with status 1.", flush=True)
        sys.exit(1)

    with open(status_path, "r", encoding="utf-8") as sf:
        status_data = json.load(sf)

    # Agar post_to_answer_found True hai, toh smoothly exit 0 kar jana hai
    if status_data.get("post_to_answer_found") is True:
        print("[INFO] 'post_to_answer_found' is already True. Exiting script smoothly (exit status 0).", flush=True)
        sys.exit(0)
    
    print("[OK] 'post_to_answer_found' is False. Running script...", flush=True)

    # Topics file se ek random topic select karna
    selected_topic = get_random_topic(TOPICS_FILE)
    print(f"[TOPIC] Selected topic path: {selected_topic}", flush=True)

    # Cookies load aur decrypt karna
    cookies = load_cookies(Path(QUORA_COOKIES_FILE), decrypt_key)

    stealth = Stealth()
    pw_cm = stealth.use_sync(sync_playwright())
    pw = pw_cm.__enter__()

    browser = None
    try:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled"
            ]
        )

        context = browser.new_context(
            no_viewport=True,
            user_agent=USER_AGENT
        )

        context.grant_permissions(["clipboard-read", "clipboard-write"])
        context.add_cookies(cookies)

        page = context.new_page()

        # 2. Quora Home par jana
        print("[STEP] Opening Quora Home...", flush=True)
        page.goto("https://www.quora.com/", wait_until="load")
        
        # 3. Selected topic URL par navigate karna
        topic_url = f"https://www.quora.com/{selected_topic}"
        print(f"[STEP] Navigating to Topic URL: {topic_url}", flush=True)
        page.goto(topic_url, wait_until="load")
        
        # 15 to 30 seconds random wait
        custom_random_wait(15, 30)
        
        # 4. Dropdown locator ko dhoond kar click karna
        print("[STEP] Locating and clicking the share/more dropdown wrapper...", flush=True)
        dropdown_trigger = page.locator('.q-relative > .q-box.qu-display--inline-block > .q-relative > .q-click-wrapper').first
        dropdown_trigger.click()
        
        # 3 to 6 seconds wait dropdown khulne ke liye
        print("[STEP] Waiting for dropdown to render...", flush=True)
        custom_random_wait(3, 6)
        
        # 5. Dropdown ke andar "Copy link" par click karna (With Fallback)
        print("[STEP] Attempting to click 'Copy link' option...", flush=True)
        
        copy_link_btn = page.locator('div').filter(has_text=r'/^Copy link$/').first
        try:
            if copy_link_btn.is_visible(timeout=5000):
                copy_link_btn.click()
                print("[OK] Clicked 'Copy link' using primary regex locator.", flush=True)
            else:
                raise Exception("Primary locator not visible")
        except Exception:
            print("[FALLBACK] Primary locator failed. Trying get_by_text('Copy link')...", flush=True)
            fallback_btn = page.get_by_text("Copy link").first
            fallback_btn.click()
            print("[OK] Clicked 'Copy link' using fallback locator.", flush=True)
        
        # Clipboard se URL read karna
        copied_url = page.evaluate("navigator.clipboard.readText()")
        print(f"[COPIED URL] Link from clipboard: {copied_url}", flush=True)
        
        # 6. Copied link par navigate karna
        if copied_url and str(copied_url).startswith("http"):
            print(f"[STEP] Navigating to copied link...", flush=True)
            page.goto(copied_url, wait_until="load")
            
            # Copied link par jaane ke baad 15 to 30 seconds wait
            print("[STEP] Waiting after navigating to copied link...", flush=True)
            custom_random_wait(15, 30)
            
            # 7. New Locator se Text extract aur Validation check karna
            print("[STEP] Attempting to extract text from the new targeted CSS locator...", flush=True)
            target_text_locator = page.locator(".q-text.puppeteer_test_question_title").first
            
            if target_text_locator.is_visible(timeout=5000):
                raw_text = target_text_locator.inner_text()
                
                # Check agar text 30 characters se kam hai toh exit 1 dena hai
                if len(raw_text) < 30:
                    print(f"[CRITICAL] Extracted text is less than 30 chars ({len(raw_text)} chars). Exiting script with status 1.", flush=True)
                    sys.exit(1)
                
                # Text se saare newlines ko space se replace karna aur clean karna
                cleaned_content = " ".join(raw_text.replace("\n", " ").replace("\r", " ").split())
                print(f"[OK] Text validation passed and formatted successfully.", flush=True)
                
                # Locator par click karke agle page par navigate karna
                print("[STEP] Clicking on the locator to navigate to the next page...", flush=True)
                target_text_locator.click()
                page.wait_for_load_state("load")
                
                # Navigated page ka naya URL nikalna aur '?' ke baad ka text remove karna
                raw_navigated_url = page.url
                clean_navigated_url = raw_navigated_url.split("?")[0]
                print(f"[NEW URL] Navigated URL (Cleaned): {clean_navigated_url}", flush=True)
                
                # ==================================================
                # EXTRACTION GUARD: DUPLICATE CHECK IN ANSWERED.JSON
                # ==================================================
                print(f"[STEP] Checking if {clean_navigated_url} exists in {ANSWERED_JSON_FILE}...", flush=True)
                if is_link_already_answered(clean_navigated_url, ANSWERED_JSON_FILE):
                    print(f"[EXIT 1] Link is already present in {ANSWERED_JSON_FILE} (considering trailing slash mapping). Leaving status.json untouched.", flush=True)
                    sys.exit(1)
                
                print("[OK] Link is brand new, proceeding to update status.json...", flush=True)

                # ==================================================
                # STATUS JSON UPDATE (SUCCESS PATH)
                # ==================================================
                status_data["post_to_answer_found"] = True
                status_data["link_to_post_to_answer"] = clean_navigated_url
                status_data["content_of_post_to_answer"] = cleaned_content
                
                with open(status_path, "w", encoding="utf-8") as sf:
                    json.dump(status_data, sf, indent=4, ensure_ascii=False)
                print("[OK] status.json has been successfully updated.", flush=True)
                
            else:
                print("[WARNING] Targeted text locator visible nahi mila. status.json update nahi hua.", flush=True)
                
        else:
            print("[WARNING] Clipboard mein valid URL nahi mila. Process ignored.", flush=True)
        
        # Final 15 to 30 seconds wait browser close hone se pehle
        print("[STEP] Initiating final post-execution delay...", flush=True)
        custom_random_wait(15, 30)
        
        print("[SUCCESS] Process completed successfully. Closing browser.", flush=True)

    except SystemExit:
        # SystemExit triggers transparently to preserve custom exit status 0 or 1
        raise
    except Exception as e:
        print("[ERROR] Script execution broke down due to trace:", e, flush=True)
        sys.exit(1)

    finally:
        if browser:
            try:
                browser.close()
            except:
                pass

        try:
            pw_cm.__exit__(None, None, None)
        except:
            pass

        print("[DONE] Script execution environment torn down cleanly.", flush=True)


if __name__ == "__main__":
    load_dotenv()
    DECRYPT_KEY = os.getenv("DECRYPT_KEY")
    if not DECRYPT_KEY:
        raise RuntimeError("DECRYPT_KEY missing in environment variables")
    run(DECRYPT_KEY)