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
from turnstile_solver import solve


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

    formatted_cookies = []
    for c in cookies:
        selenium_cookie = {
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True)
        }
        
        if "expirationDate" in c:
            selenium_cookie["expiry"] = int(c["expirationDate"])
        elif "expiry" in c:
            selenium_cookie["expiry"] = int(c["expiry"])

        if "sameSite" in c:
            val = str(c["sameSite"]).lower()
            if val in ["no_restriction", "none", "unspecified", "null"]:
                selenium_cookie["sameSite"] = "None"
            elif val == "lax":
                selenium_cookie["sameSite"] = "Lax"
            elif val == "strict":
                selenium_cookie["sameSite"] = "Strict"
            else:
                selenium_cookie["sameSite"] = "Lax"
                
        formatted_cookies.append(selenium_cookie)

    print("[OK] Cookies loaded successfully for Selenium", flush=True)
    return formatted_cookies


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
                
            def normalize(link: str) -> str:
                return str(link).strip().rstrip("/")
                
            target_norm = normalize(url)
            for existing_link in answered_list:
                if normalize(existing_link) == target_norm:
                    return True
    except Exception as e:
        print(f"[WARNING] Error reading {answered_file}: {e}", flush=True)
        
    return False

def upload_to_tmpfiles(screenshot_path):
    url = "https://tmpfiles.org/api/v1/upload"
    
    with open(screenshot_path, "rb") as file:
        response = requests.post(url, files={"file": file})
        
    if response.status_code == 200:
        res_data = response.json()
        # Direct view URL banane ke liye '/dl/' replace karte hain
        page_url = res_data["data"]["url"]
        direct_url = page_url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
        print(f"👉 DIRECT LINK (Expires in 2 Hours): {direct_url}")
        return direct_url
    else:
        print(f"[WARNING] Upload Failed: {response.status_code}")
        return None

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

    if status_data.get("post_to_answer_found") is True:
        print("[INFO] 'post_to_answer_found' is already True. Exiting script smoothly (exit status 0).", flush=True)
        sys.exit(0)
    
    print("[OK] 'post_to_answer_found' is False. Running script...", flush=True)

    selected_topic = get_random_topic(TOPICS_FILE)
    print(f"[TOPIC] Selected topic path: {selected_topic}", flush=True)

    cookies = load_cookies(Path(QUORA_COOKIES_FILE), decrypt_key)

    driver = None
    try:
        # Initialize SeleniumBase with Undetected Chrome context
        driver = Driver(uc=True, headless=HEADLESS, agent=USER_AGENT)
        driver.maximize_window()

        # Cookie injection ke liye domain setup zaroori hai
        print("[STEP] Initializing domain context for cookies...", flush=True)
        driver.get("https://www.quora.com")
        time.sleep(2)
        
        print("[STEP] Adding cookies to browser context...", flush=True)
        for cookie in cookies:
            try:
                driver.add_cookie(cookie)
            except Exception:
                pass
        print("[OK] Cookies added successfully", flush=True)

        # 2. Quora Home par jana
        print("[STEP] Opening Quora Home...", flush=True)
        driver.get("https://www.quora.com/")
        
        # Turnstile check right after homepage access
        print("[STEP] Checking for Cloudflare barriers on Home...", flush=True)
        solve(driver, detect_timeout=5, solve_timeout=30, verify=True)
        
        # 3. Selected topic URL par navigate karna
        topic_url = f"https://www.quora.com/{selected_topic}/top_questions"
        print(f"[STEP] Navigating to Topic URL: {topic_url}", flush=True)
        driver.get(topic_url)
        
        # Turnstile check right after topic navigation
        print("[STEP] Checking for Cloudflare barriers on Topic URL...", flush=True)
        solve(driver, detect_timeout=5, solve_timeout=30, verify=True)
        
        custom_random_wait(15, 30)
        
        # ========================================================
        # NEW DIRECT LINK EXTRACTION (NO DROPDOWN / NO CLIPBOARD)
        # ========================================================
        print("[STEP] Extracting target link directly from the page...", flush=True)
        wait = WebDriverWait(driver, 30)
        
        # Aapka diya hua exact absolute target text locator
        target_span_xpath = "/html[1]/body[1]/div[2]/div[1]/div[2]/div[1]/div[3]/div[1]/div[1]/div[2]/div[4]/div[1]/div[1]/div[1]/div[1]/div[1]/div[1]/div[1]/div[1]/div[1]/div[1]/div[1]/div[1]/div[1]/div[1]/span[1]/span[1]/a[1]/div[1]/div[1]/div[1]/div[1]/span[1]/span[1]"
        
        # Is text ke parent standard <a> tag ko locate karna URL extract karne ke liye
        parent_link_xpath = f"{target_span_xpath}/ancestor::a"
        
        target_span_el = wait.until(EC.visibility_of_element_located((By.XPATH, target_span_xpath)))
        parent_link_el = driver.find_element(By.XPATH, parent_link_xpath)
        
        # Content aur Href link extract karna
        raw_text = target_span_el.text
        copied_url = parent_link_el.get_attribute("href")
        
        print(f"[EXTRACTED URL] Href link found: {copied_url}", flush=True)
        
        # 6. Copied link par navigate karna
        if copied_url and str(copied_url).startswith("http"):
            print(f"[STEP] Navigating to extracted link...", flush=True)
            driver.get(copied_url)
            
            print("[STEP] Checking for Cloudflare barriers on Target link...", flush=True)
            solve(driver, detect_timeout=5, solve_timeout=30, verify=True)
            
            print("[STEP] Waiting after navigating to extracted link...", flush=True)
            custom_random_wait(15, 30)
            
            # Text Validation check
            if len(raw_text) < 30:
                print(f"[CRITICAL] Extracted text is less than 30 chars ({len(raw_text)} chars). Exiting script with status 1.", flush=True)
                if 'driver' in locals() and driver:
                    try:
                        screenshot_path = "error_screenshot.png"
                        driver.save_screenshot(screenshot_path)
                        print(f"[OK] Error screenshot captured: {screenshot_path}", flush=True)
                        
                        upload_to_tmpfiles(screenshot_path)
                    except Exception as screenshot_err:
                        print(f"[WARNING] Could not capture or upload screenshot: {screenshot_err}", flush=True)
                sys.exit(1)
            
            cleaned_content = " ".join(raw_text.replace("\n", " ").replace("\r", " ").split())
            print(f"[OK] Text validation passed and formatted successfully.", flush=True)
            
            # Navigated URL filtering
            raw_navigated_url = driver.current_url
            clean_navigated_url = raw_navigated_url.split("?")[0]
            print(f"[NEW URL] Current URL (Cleaned): {clean_navigated_url}", flush=True)

            if not clean_navigated_url.startswith("https://www.quora.com"):
                print(f"[CRITICAL] URL '{clean_navigated_url}' Quora domain se shuru nahi ho raha hai! Exiting script with status 1.", flush=True)
                if 'driver' in locals() and driver:
                    try:
                        screenshot_path = "error_screenshot.png"
                        driver.save_screenshot(screenshot_path)
                        print(f"[OK] Error screenshot captured: {screenshot_path}", flush=True)
                        
                        upload_to_tmpfiles(screenshot_path)
                    except Exception as screenshot_err:
                        print(f"[WARNING] Could not capture or upload screenshot: {screenshot_err}", flush=True)
                sys.exit(1)
            
            print(f"[STEP] Checking if {clean_navigated_url} exists in {ANSWERED_JSON_FILE}...", flush=True)
            if is_link_already_answered(clean_navigated_url, ANSWERED_JSON_FILE):
                print(f"[EXIT 1] Link is already present in {ANSWERED_JSON_FILE}. Leaving status.json untouched.", flush=True)
                if 'driver' in locals() and driver:
                    try:
                        screenshot_path = "error_screenshot.png"
                        driver.save_screenshot(screenshot_path)
                        print(f"[OK] Error screenshot captured: {screenshot_path}", flush=True)
                        
                        upload_to_tmpfiles(screenshot_path)
                    except Exception as screenshot_err:
                        print(f"[WARNING] Could not capture or upload screenshot: {screenshot_err}", flush=True)
                sys.exit(1)
            
            print("[OK] Link is brand new, proceeding to update status.json...", flush=True)

            # STATUS JSON UPDATE
            status_data["post_to_answer_found"] = True
            status_data["link_to_post_to_answer"] = clean_navigated_url
            status_data["content_of_post_to_answer"] = cleaned_content
            
            with open(status_path, "w", encoding="utf-8") as sf:
                json.dump(status_data, sf, indent=4, ensure_ascii=False)
            print("[OK] status.json has been successfully updated.", flush=True)
                
        else:
            print("[WARNING] Valid URL extract nahi ho paaya. Process ignored.", flush=True)
        
        print("[STEP] Initiating final post-execution delay...", flush=True)
        custom_random_wait(15, 30)
        print("[SUCCESS] Process completed successfully. Closing browser.", flush=True)

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
                
                upload_to_tmpfiles(screenshot_path)

            except Exception as screenshot_err:
                print(f"[WARNING] Could not capture screenshot or upload: {screenshot_err}", flush=True)
        sys.exit(1)

    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
        print("[DONE] Script execution environment torn down cleanly.", flush=True)


if __name__ == "__main__":
    load_dotenv()
    DECRYPT_KEY = os.getenv("DECRYPT_KEY")
    if not DECRYPT_KEY:
        raise RuntimeError("DECRYPT_KEY missing in environment variables")
    run(DECRYPT_KEY)