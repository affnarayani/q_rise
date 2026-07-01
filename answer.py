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
STATUS_JSON_FILE = "status.json"
ANSWERED_JSON_FILE = "answered.json"

PBKDF2_ITERATIONS = 200_000

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


# =========================
# RANDOM WAIT
# =========================
def custom_random_wait(min_sec=15, max_sec=30):
    seconds = random.uniform(min_sec, max_sec)
    print(f"[WAIT] Sleeping for {seconds:.2f} seconds...", flush=True)
    time.sleep(seconds)


# =========================
# CRYPTO
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


def load_cookies(file_path: Path) -> List[Dict[str, Any]]:
    print("[STEP] Loading cookies...", flush=True)

    with file_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    plaintext = _decrypt_payload(payload, DECRYPT_KEY)
    cookies = json.loads(plaintext.decode("utf-8"))

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

    print("[OK] Cookies loaded", flush=True)
    return cookies


# =========================
# ENV VALIDATION
# =========================
load_dotenv()

DECRYPT_KEY = os.getenv("DECRYPT_KEY")

if not DECRYPT_KEY:
    raise RuntimeError("DECRYPT_KEY missing")


# =========================
# MAIN
# =========================
def run():
    print("[START] Script started", flush=True)

    # ========================================================
    # STATUS JSON VALIDATION
    # ========================================================
    status_path = Path(STATUS_JSON_FILE)
    if not status_path.exists():
        raise FileNotFoundError(f"❌ Status file {STATUS_JSON_FILE} not found!")

    with status_path.open("r", encoding="utf-8") as sf:
        status_data = json.load(sf)

    answer_gen = status_data.get("answer_generated")
    answer_to_post = status_data.get("answer")
    target_url = status_data.get("link_to_post_to_answer")

    # Condition: "answer_generated" must be true AND "answer" key must not be empty
    if not (answer_gen is True and answer_to_post and str(answer_to_post).strip() != ""):
        print("[EXIT] Script will not run: 'answer_generated' is not True or 'answer' is empty.", flush=True)
        sys.exit(0)

    if not target_url:
        print("[ERROR] QUORA URL missing in status.json.", flush=True)
        sys.exit(1)

    # Cookies setup
    cookies = load_cookies(Path(QUORA_COOKIES_FILE))
    print(f"[OK] Total cookies loaded: {len(cookies)}", flush=True)

    # =========================
    # STEALTH SETUP
    # =========================
    stealth = Stealth()
    pw_cm = stealth.use_sync(sync_playwright())
    pw = pw_cm.__enter__()

    browser = None
    try:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            ignore_default_args=["--enable-automation"],
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-breakpad",
                "--disable-client-side-phishing-detection",
                "--disable-default-apps",
                "--disable-dev-shm-usage"
            ]
        )

        context = browser.new_context(
            no_viewport=True,
            user_agent=USER_AGENT,
            extra_http_headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "accept-language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1"
            }
        )

        context.grant_permissions(["clipboard-read", "clipboard-write"])
        print("[STEP] Adding cookies to browser context...", flush=True)
        context.add_cookies(cookies)

        page = context.new_page()
        print("[OK] Cookies added successfully", flush=True)

        # ========================================================
        # DIRECT NAVIGATION TO QUORA URL
        # ========================================================
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {}, loadTimes: Date.now, csi: () => {} };
        """)

        print(f"[STEP] Navigating directly to QUORA post URL: {target_url}...", flush=True)
        
        # 'commit' use karein taaki jaise hi network stream shuru ho, script capture karna shuru kare
        page.goto(target_url, wait_until="commit")

        # Fixed static wait hata kar hum thoda rukaav denge page elements build hone ke liye
        time.sleep(15) 

        # Content aur Frame detection check
        page_content = page.content()
        if "Performing security verification" in page_content or "challenge" in page_content.lower():
            print("[!_!] Security Challenge Screen detected in HTML stream. Triggering physical coordinate mouse overrides...", flush=True)
            try:
                # Custom coordinates grid simulation jahan default turnstile standard templates align hote hain
                # 1920x1080 screen optimization ke hisab se coordinates set kiye hain
                grid_clicks = [(450, 480), (500, 500), (960, 540)] # Center and left panel points
                
                for x, y in grid_clicks:
                    print(f"[*] Moving and dispatching hardware click pulse at coordinate: ({x}, {y})", flush=True)
                    page.mouse.move(x, y)
                    page.mouse.down()
                    time.sleep(0.05)
                    page.mouse.up()
                    time.sleep(2)
                
                # Check response post-clicks
                time.sleep(5)
            except Exception as e:
                print(f"[-] Hardware coordinate matrix override failed: {e}", flush=True)

        print(f"[OK] {target_url} opened completely", flush=True)
        custom_random_wait(15, 30)

        # ========================================================
        # CLICK "Answer ·" BUTTON
        # ========================================================
        print("[STEP] Locating 'Answer' button...", flush=True)
        locator_primary = page.get_by_role('button', name='Answer ·')
        locator_secondary = page.get_by_role('button', name='Answer')
        answer_btn = locator_primary.or_(locator_secondary).first
        answer_btn.wait_for(state="visible", timeout=30000)
        answer_btn.click()
        print("[OK] 'Answer' button clicked.", flush=True)

        # Wait for 15, 30 seconds for the pop-up to fully load
        custom_random_wait(15, 30)

        # ========================================================
        # LOCATE POP-UP TEXT FIELD & TYPE ANSWER
        # ========================================================
        print("[STEP] Locating text editor field inside pop-up...", flush=True)
        
        # Specific selector check, falls back to generic editor if dark_mode class differs
        editor_field = page.locator(".doc.dark_mode.empty.focus-visible, .doc.empty").first
        editor_field.wait_for(state="visible", timeout=15000)
        editor_field.click()
        print("[OK] Editor field focused.", flush=True)

        print("[STEP] Typing answer via native keyboard emulation...", flush=True)
        for char in answer_to_post:
            page.keyboard.type(char)
            time.sleep(random.uniform(0.04, 0.09))
        print("[OK] Typing completed.", flush=True)

        # Wait 15, 30 seconds after typing
        custom_random_wait(15, 30)

        # ========================================================
        # CLICK POST BUTTON
        # ========================================================
        print("[STEP] Clicking 'Post' button...", flush=True)
        
        # Trying Role first, if hidden/fails, trying via text matching XPath
        post_btn = page.get_by_role('button', name='Post')
        try:
            post_btn.wait_for(state="visible", timeout=5000)
            post_btn.click()
        except:
            print("[INFO] Role button failed, trying alternative text locator...", flush=True)
            post_btn_alt = page.locator("//div[contains(text(),'Post')]").first
            post_btn_alt.wait_for(state="visible", timeout=10000)
            post_btn_alt.click()
            
        print("[OK] Answer posted successfully!", flush=True)

        # Post button click karne ke baad ka obligatory 15, 30 sec wait
        custom_random_wait(15, 30)

        # ========================================================
        # OPTIONAL: CLICK "Done" BUTTON (IF VISIBLE)
        # ========================================================
        print("[STEP] Checking if 'Done' button is visible...", flush=True)
        try:
            done_btn = page.get_by_role('button', name='Done')
            # 5 seconds wait to see if it shows up
            done_btn.wait_for(state="visible", timeout=5000)
            done_btn.click()
            print("[OK] 'Done' button clicked successfully.", flush=True)
            # Done click hone ke baad firse 15, 30 sec wait
            custom_random_wait(15, 30)
        except Exception:
            print("[INFO] 'Done' button not found or not visible. Skipping this step.", flush=True)

        # ========================================================
        # APPEND TO ANSWERED.JSON (FROM TOP)
        # ========================================================
        print(f"[STEP] Appending link to top of {ANSWERED_JSON_FILE}...", flush=True)
        answered_path = Path(ANSWERED_JSON_FILE)
        answered_list = []
        
        if answered_path.exists():
            try:
                with answered_path.open("r", encoding="utf-8") as af:
                    answered_list = json.load(af)
                    if not isinstance(answered_list, list):
                        answered_list = []
            except Exception:
                answered_list = []
                
        # Insert target URL at the 0th index (top)
        if target_url not in answered_list:
            answered_list.insert(0, target_url)
            
        with answered_path.open("w", encoding="utf-8") as af:
            json.dump(answered_list, af, indent=4)
        print("[OK] URL saved to answered.json", flush=True)

        # ========================================================
        # RESET STATUS JSON ON SUCCESSFUL RUN
        # ========================================================
        print(f"[STEP] Resetting all keys in {STATUS_JSON_FILE}...", flush=True)
        status_data["post_to_answer_found"] = False
        status_data["link_to_post_to_answer"] = ""
        status_data["content_of_post_to_answer"] = ""
        status_data["answer_generated"] = False
        status_data["answer"] = ""
        
        with status_path.open("w", encoding="utf-8") as sf:
            json.dump(status_data, sf, indent=4)
        print("[OK] status.json reset complete.", flush=True)

        # Final hold before closing browser context (15, 30 seconds)
        print("[STEP] Final hold before closing browser context...", flush=True)
        custom_random_wait(15, 30)

    except SystemExit:
        raise
    except Exception as e:
        print("[CRITICAL ERROR] Automation pipeline failed or locator timed out:", e, flush=True)
        if 'page' in locals() and page:
            try:
                screenshot_path = "error_screenshot.png"
                page.screenshot(path=screenshot_path, full_page=True)
                print(f"[OK] Error screenshot captured: {screenshot_path}", flush=True)
            except Exception as screenshot_err:
                print(f"[WARNING] Could not capture screenshot: {screenshot_err}", flush=True)
        sys.exit(1)

    finally:
        if browser:
            try:
                browser.close()
                print("[OK] Browser closed context safely.", flush=True)
            except:
                pass
        try:
            pw_cm.__exit__(None, None, None)
        except:
            pass

        print("[DONE] Process terminated cleanly.", flush=True)


if __name__ == "__main__":
    run()