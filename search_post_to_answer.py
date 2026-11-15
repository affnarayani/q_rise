import os
import random
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from common.config import get_path, load_config
from common.locators import apply_auto_correct_if_enabled
from common.locator_runtime import run_with_locator, try_with_locator
from common.utils import (
    custom_random_wait,
    load_playwright_cookies,
    pick_first_encrypted_cookie,
    read_json,
    save_error_screenshot,
    try_solve_turnstile,
    write_json,
)


def get_random_topic(file_path: Path) -> str:
    if not file_path.exists():
        print(f"[CRITICAL] {file_path.as_posix()} file not found! Exiting.", flush=True)
        sys.exit(1)
    with file_path.open("r", encoding="utf-8") as f:
        topics = [line.strip() for line in f if line.strip()]
    if not topics:
        print(f"[CRITICAL] {file_path.as_posix()} is empty! Exiting.", flush=True)
        sys.exit(1)
    return random.choice(topics)


def is_link_already_answered(url: str, answered_file: Path) -> bool:
    answered_list = read_json(answered_file, [])
    if not isinstance(answered_list, list):
        return False

    def normalize(link: str) -> str:
        return str(link).strip().rstrip("/")

    target_norm = normalize(url)
    for existing_link in answered_list:
        if normalize(str(existing_link)) == target_norm:
            return True
    return False


def run(decrypt_key: str):
    print("[START] Script started", flush=True)
    load_dotenv()
    config = load_config()
    apply_auto_correct_if_enabled(config)

    status_path = get_path(config, "status_file")
    answered_path = get_path(config, "answered_file")
    topics_path = get_path(config, "topics_file")
    quora_cookies_dir = get_path(config, "quora_cookies_dir")

    status_data = read_json(status_path, None)
    if status_data is None or not isinstance(status_data, dict):
        print(f"[CRITICAL] {status_path.as_posix()} not found or invalid. Exiting with status 1.", flush=True)
        sys.exit(1)

    if status_data.get("post_to_answer_found") is True:
        print("[INFO] 'post_to_answer_found' is already True. Exiting script smoothly (exit status 0).", flush=True)
        sys.exit(0)

    print("[OK] 'post_to_answer_found' is False. Running script...", flush=True)

    selected_topic = get_random_topic(topics_path)
    print(f"[TOPIC] Selected topic path: {selected_topic}", flush=True)

    cookie_file = pick_first_encrypted_cookie(quora_cookies_dir)
    cookies = load_playwright_cookies(cookie_file, decrypt_key, config["pbkdf2_iterations"])
    print("[OK] Cookies loaded successfully for Playwright", flush=True)

    stealth = Stealth()
    pw_cm = stealth.use_sync(sync_playwright())
    pw = pw_cm.__enter__()
    browser = None
    page = None
    try:
        browser = pw.chromium.launch(
            headless=bool(config.get("headless", True)),
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(no_viewport=True, user_agent=config.get("user_agent", ""))
        context.grant_permissions(["clipboard-read", "clipboard-write"])
        print("[STEP] Adding cookies to browser context...", flush=True)
        context.add_cookies(cookies)
        page = context.new_page()
        print("[OK] Cookies added successfully", flush=True)

        print("[STEP] Opening Quora Home...", flush=True)
        page.goto("https://www.quora.com/", wait_until="load")
        try_solve_turnstile(page, timeout_sec=25)

        topic_url = f"https://www.quora.com/{selected_topic}/top_questions"
        print(f"[STEP] Navigating to Topic URL: {topic_url}", flush=True)
        page.goto(topic_url, wait_until="load")
        try_solve_turnstile(page, timeout_sec=25)
        custom_random_wait(config, "absolute")

        print("[STEP] Extracting target link directly from the page...", flush=True)
        pw_context = {"page": page}

        raw_text = run_with_locator(
            config, "quora", "quora_target_question_text_span", pw_context,
            "Extract target question text from topic page",
            lambda obj: obj.first.inner_text(timeout=15000),
        )
        copied_url = run_with_locator(
            config, "quora", "quora_target_question_parent_link", pw_context,
            "Extract target question link href from topic page",
            lambda obj: obj.first.get_attribute("href", timeout=15000),
        )

        print(f"[EXTRACTED URL] Href link found: {copied_url}", flush=True)

        if copied_url and str(copied_url).startswith("http"):
            print("[STEP] Navigating to extracted link...", flush=True)
            page.goto(str(copied_url), wait_until="load")
            try_solve_turnstile(page, timeout_sec=25)
            print("[STEP] Waiting after navigating to extracted link...", flush=True)
            custom_random_wait(config, "absolute")

            if len(str(raw_text)) < 30:
                print(f"[CRITICAL] Extracted text is less than 30 chars ({len(str(raw_text))} chars). Exiting script with status 1.", flush=True)
                if page:
                    save_error_screenshot(page, config)
                sys.exit(1)

            cleaned_content = " ".join(str(raw_text).replace("\n", " ").replace("\r", " ").split())
            print("[OK] Text validation passed and formatted successfully.", flush=True)

            raw_navigated_url = page.url
            clean_navigated_url = raw_navigated_url.split("?")[0]
            print(f"[NEW URL] Current URL (Cleaned): {clean_navigated_url}", flush=True)

            if not clean_navigated_url.startswith("https://www.quora.com"):
                print(f"[CRITICAL] URL '{clean_navigated_url}' does not start with Quora domain! Exiting script with status 1.", flush=True)
                if page:
                    save_error_screenshot(page, config)
                sys.exit(1)

            print(f"[STEP] Checking if {clean_navigated_url} exists in {answered_path.as_posix()}...", flush=True)
            if is_link_already_answered(clean_navigated_url, answered_path):
                print(f"[EXIT 1] Link is already present in {answered_path.as_posix()}. Leaving status.json untouched.", flush=True)
                if page:
                    save_error_screenshot(page, config)
                sys.exit(1)

            print("[OK] Link is brand new, proceeding to update status.json...", flush=True)
            status_data["post_to_answer_found"] = True
            status_data["link_to_post_to_answer"] = clean_navigated_url
            status_data["content_of_post_to_answer"] = cleaned_content
            write_json(status_path, status_data)
            print("[OK] status.json has been successfully updated.", flush=True)
        else:
            print("[WARNING] Valid URL could not be extracted. Process ignored.", flush=True)

        print("[STEP] Initiating final post-execution delay...", flush=True)
        custom_random_wait(config, "absolute")
        print("[SUCCESS] Process completed successfully. Closing browser.", flush=True)

    except SystemExit:
        raise
    except Exception as e:
        print("\n" + "!" * 60, flush=True)
        print(f"[CRITICAL ERROR] Automation pipeline failed: {e}", flush=True)
        print("!" * 60 + "\n", flush=True)
        if page:
            save_error_screenshot(page, config)
        sys.exit(1)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        try:
            pw_cm.__exit__(None, None, None)
        except Exception:
            pass
        print("[DONE] Script execution environment torn down cleanly.", flush=True)


if __name__ == "__main__":
    load_dotenv()
    DECRYPT_KEY = os.getenv("DECRYPT_KEY")
    if not DECRYPT_KEY:
        raise RuntimeError("DECRYPT_KEY missing in environment variables")
    run(DECRYPT_KEY)
