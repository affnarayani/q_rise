import os
import sys
import json
import time
import base64
import random
from pathlib import Path
from typing import List, Dict, Any
import requests
from dotenv import load_dotenv

from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

# SeleniumBase and Turnstile Solver Imports
from seleniumbase import Driver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from turnstile_solver import solve


# =========================
# CONFIG
# =========================
HEADLESS = True  # Note: UC Mode works best when False, but turnstile_solver can attempt headless execution

QUORA_COOKIES_FILE = "quora_cookies.json.encrypted"
STATUS_JSON_FILE = "status.json"
ANSWERED_JSON_FILE = "answered.json"

PBKDF2_ITERATIONS = 200_000

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/122.0.0.0 Safari/537.36"


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

    formatted_cookies = []
    for c in cookies:
        # Format cookie parameters to match Selenium requirements
        selenium_cookie = {
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True)
        }
        
        # Handle expiry mapping safely
        if "expirationDate" in c:
            selenium_cookie["expiry"] = int(c["expirationDate"])
        elif "expiry" in c:
            selenium_cookie["expiry"] = int(c["expiry"])

        # Handle SameSite configuration
        if "sameSite" in c:
            val = str(c["sameSite"]).lower()
            if val in ["no_restriction", "none"]:
                selenium_cookie["sameSite"] = "None"
            elif val == "lax":
                selenium_cookie["sameSite"] = "Lax"
            elif val == "strict":
                selenium_cookie["sameSite"] = "Strict"
                
        formatted_cookies.append(selenium_cookie)

    print("[OK] Cookies loaded and formatted for Selenium", flush=True)
    return formatted_cookies


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

    if not (answer_gen is True and answer_to_post and str(answer_to_post).strip() != ""):
        print("[EXIT] Script will not run: 'answer_generated' is not True or 'answer' is empty.", flush=True)
        sys.exit(0)

    if not target_url:
        print("[ERROR] QUORA URL missing in status.json.", flush=True)
        sys.exit(1)

    cookies = load_cookies(Path(QUORA_COOKIES_FILE))
    print(f"[OK] Total cookies loaded: {len(cookies)}", flush=True)

    # =========================
    # SELENIUMBASE DRIVER SETUP
    # =========================
    driver = None
    try:
        # Initialize SeleniumBase with Undetected Chrome (uc=True)
        driver = Driver(uc=True, headless=HEADLESS, agent=USER_AGENT)
        driver.maximize_window()

        # To add cookies in Selenium, you must be on the domain first
        print("[STEP] Initializing domain context for cookies...", flush=True)
        driver.get("https://www.quora.com")
        time.sleep(2)
        
        print("[STEP] Adding cookies to browser context...", flush=True)
        for cookie in cookies:
            try:
                driver.add_cookie(cookie)
            except Exception as ce:
                # Catch dynamic top-level mismatch errors silently if any
                pass
        print("[OK] Cookies added successfully", flush=True)

        # ========================================================
        # DIRECT NAVIGATION TO QUORA URL
        # ========================================================
        print(f"[STEP] Navigating directly to QUORA post URL: {target_url}...", flush=True)
        driver.get(target_url)
        print(f"[OK] {target_url} opened completely", flush=True)
        
        # ========================================================
        # TURNSTILE SOLVER INTEGRATION
        # ========================================================
        print("[STEP] Checking for Cloudflare Turnstile barriers...", flush=True)
        solved = solve(
            driver,
            detect_timeout=7,
            solve_timeout=35,
            interval=1,
            verify=True,
            click_method="cdp",
            theme="auto"
        )
        print(f"[INFO] Turnstile solver completion status: {solved}", flush=True)

        # Post navigation hold
        custom_random_wait(15, 30)

        # ========================================================
        # CLICK "Answer" BUTTON
        # ========================================================
        print("[STEP] Locating 'Answer' button...", flush=True)
        driver.execute_script("window.scrollBy(500, 0);")
        # XPath matching both "Answer ·" and "Answer" text variants
        answer_xpath = "//button[normalize-space()='Answer'] | //div[normalize-space()='Answer']"
        wait = WebDriverWait(driver, 30)
        
        answer_btn = wait.until(EC.element_to_be_clickable((By.XPATH, answer_xpath)))
        driver.execute_script("arguments[0].click();", answer_btn)
        print("[OK] 'Answer' button clicked.", flush=True)

        custom_random_wait(15, 30)

        # ========================================================
        # LOCATE POP-UP TEXT FIELD & TYPE ANSWER
        # ========================================================
        print("[STEP] Locating text editor field inside pop-up...", flush=True)
        editor_xpath = "//*[contains(@class, 'doc') and (contains(@class, 'dark_mode') or contains(@class, 'empty'))]"
        editor_field = wait.until(EC.element_to_be_clickable((By.XPATH, editor_xpath)))
        editor_field.click()
        print("[OK] Editor field focused.", flush=True)

        print("[STEP] Typing answer via native keyboard emulation...", flush=True)
        # Type with artificial latency per character
        actions = ActionChains(driver)
        for char in answer_to_post:
            actions.send_keys(char).perform()
            time.sleep(random.uniform(0.04, 0.09))
        print("[OK] Typing completed.", flush=True)

        custom_random_wait(15, 30)

        # ========================================================
        # CLICK POST BUTTON
        # ========================================================
        print("[STEP] Clicking 'Post' button...", flush=True)
        
        post_xpath = "//button[descendant::*[text()='Post']] | //div[text()='Post']"
        try:
            post_btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, post_xpath)))
            post_btn.click()
        except Exception:
            print("[INFO] Primary locator failed, trying alternative structural search...", flush=True)
            post_btn_alt = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'Post')]"))
            )
            post_btn_alt.click()
            
        print("[OK] Answer posted successfully!", flush=True)

        custom_random_wait(15, 30)

        # ========================================================
        # OPTIONAL: CLICK "Done" BUTTON (IF VISIBLE)
        # ========================================================
        print("[STEP] Checking if 'Done' button is visible...", flush=True)
        try:
            done_xpath = "//button[descendant::*[text()='Done']] | //div[text()='Done'] | //*[text()='Done']"
            done_btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, done_xpath)))
            done_btn.click()
            print("[OK] 'Done' button clicked successfully.", flush=True)
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

        print("[STEP] Final hold before closing browser context...", flush=True)
        custom_random_wait(15, 30)

    except SystemExit:
        raise
    except Exception as e:
        print("\n" + "!"*60, flush=True)
        print(f"[CRITICAL ERROR] Automation pipeline failed: {e}", flush=True)
        print("!"*60 + "\n", flush=True)
        
        if driver:
            try:
                screenshot_path = "error_screenshot.png"
                driver.save_screenshot(screenshot_path)
                print(f"[OK] Error screenshot captured: {screenshot_path}", flush=True)
                
                imgbb_key = os.getenv("IMGBBB_API_KEY")
                if imgbb_key:
                    print("[OK] Uploading screenshot to ImgBB...", flush=True)
                    url = f"https://api.imgbb.com/1/upload?expiration=86400&key={imgbb_key}"
                    
                    with open(screenshot_path, "rb") as file:
                        response = requests.post(url, files={"image": file})
                    
                    if response.status_code == 200:
                        res_data = response.json()
                        direct_url = res_data["data"]["display_url"]
                        print("\n" + "="*50, flush=True)
                        print(f"👉 DIRECT SCREENSHOT LINK: {direct_url}", flush=True)
                        print("="*50 + "\n", flush=True)
                    else:
                        print(f"[WARNING] ImgBB Upload Failed Status: {response.status_code}", flush=True)
                else:
                    print("[WARNING] IMGBBB_API_KEY environment variable not found.", flush=True)

            except Exception as screenshot_err:
                print(f"[WARNING] Could not capture screenshot or upload: {screenshot_err}", flush=True)
        sys.exit(1)

    finally:
        if driver:
            try:
                driver.quit()
                print("[OK] Browser closed context safely.", flush=True)
            except:
                pass

        print("[DONE] Process terminated cleanly.", flush=True)


if __name__ == "__main__":
    run()