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


def run():
    print("[START] Script started", flush=True)
    load_dotenv()
    config = load_config()
    apply_auto_correct_if_enabled(config)

    status_path = get_path(config, "status_file")
    answered_path = get_path(config, "answered_file")
    quora_cookies_dir = get_path(config, "quora_cookies_dir")

    status_data = read_json(status_path, None)
    if status_data is None or not isinstance(status_data, dict):
        raise FileNotFoundError(f"Status file {status_path.as_posix()} not found or invalid!")

    answer_gen = status_data.get("answer_generated")
    answer_to_post = status_data.get("answer")
    target_url = status_data.get("link_to_post_to_answer")

    if not (answer_gen is True and answer_to_post and str(answer_to_post).strip() != ""):
        print("[EXIT] Script will not run: 'answer_generated' is not True or 'answer' is empty.", flush=True)
        sys.exit(0)

    if not target_url:
        print("[ERROR] QUORA URL missing in status.json.", flush=True)
        sys.exit(1)

    decrypt_key = os.getenv("DECRYPT_KEY")
    if not decrypt_key:
        raise RuntimeError("DECRYPT_KEY missing")
    cookie_file = pick_first_encrypted_cookie(quora_cookies_dir)
    cookies = load_playwright_cookies(cookie_file, decrypt_key, config["pbkdf2_iterations"])
    print(f"[OK] Total cookies loaded: {len(cookies)}", flush=True)

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

        print(f"[STEP] Navigating directly to QUORA post URL: {target_url}...", flush=True)
        page.goto(str(target_url), wait_until="load")
        print(f"[OK] {target_url} opened completely", flush=True)
        try_solve_turnstile(page, timeout_sec=30)
        custom_random_wait(config, "absolute")

        pw_context = {"page": page}

        print("[STEP] Locating 'Answer' button...", flush=True)
        try:
            page.mouse.wheel(500, 0)
        except Exception:
            pass
        run_with_locator(
            config, "quora", "quora_answer_button", pw_context,
            "Locate and click Answer button on Quora question page",
            lambda obj: obj.first.click(timeout=10000),
        )
        print("[OK] 'Answer' button clicked.", flush=True)
        custom_random_wait(config, "absolute")

        print("[STEP] Locating text editor field inside pop-up...", flush=True)
        run_with_locator(
            config, "quora", "quora_answer_editor_field", pw_context,
            "Locate and focus Quora answer editor field",
            lambda obj: obj.first.click(timeout=10000),
        )
        print("[OK] Editor field focused.", flush=True)

        print("[STEP] Typing answer via keyboard emulation...", flush=True)
        for char in str(answer_to_post):
            page.keyboard.type(char, delay=random.uniform(40, 90))
        print("[OK] Typing completed.", flush=True)
        custom_random_wait(config, "absolute")

        print("[STEP] Clicking 'Post' button...", flush=True)
        ok_post, _ = try_with_locator(
            config, "quora", "quora_post_answer_button", pw_context,
            "Locate and click Post button to submit answer",
            lambda obj: obj.first.click(timeout=10000),
            allow_vision=True
        )
        if not ok_post:
            print("[CRITICAL] Could not click 'Post' button after locator attempts and vision pass.", flush=True)
            if page:
                save_error_screenshot(page, config)
            sys.exit(1)
        print("[OK] Answer posted successfully!", flush=True)
        custom_random_wait(config, "absolute")

        print("[STEP] Checking if 'Done' button is visible...", flush=True)
        ok_done, _ = try_with_locator(
            config, "quora", "quora_done_button", pw_context,
            "Locate and click Done button after posting answer if visible",
            lambda obj: obj.first.click(timeout=5000),
            allow_vision=False
        )
        if ok_done:
            print("[OK] 'Done' button clicked successfully.", flush=True)
            custom_random_wait(config, "absolute")
        else:
            print("[INFO] 'Done' button not found or not clickable. Skipping this step.", flush=True)

        print(f"[STEP] Appending link to top of {answered_path.as_posix()}...", flush=True)
        answered_list = read_json(answered_path, [])
        if not isinstance(answered_list, list):
            answered_list = []
        if target_url not in answered_list:
            answered_list.insert(0, target_url)
        write_json(answered_path, answered_list)
        print("[OK] URL saved to answered.json", flush=True)

        print(f"[STEP] Resetting all keys in {status_path.as_posix()}...", flush=True)
        status_data["post_to_answer_found"] = False
        status_data["link_to_post_to_answer"] = ""
        status_data["content_of_post_to_answer"] = ""
        status_data["answer_generated"] = False
        status_data["answer"] = ""
        write_json(status_path, status_data)
        print("[OK] status.json reset complete.", flush=True)

        print("[STEP] Final hold before closing browser context...", flush=True)
        custom_random_wait(config, "absolute")

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
                print("[OK] Browser closed context safely.", flush=True)
            except Exception:
                pass
        try:
            pw_cm.__exit__(None, None, None)
        except Exception:
            pass
        print("[DONE] Process terminated cleanly.", flush=True)


if __name__ == "__main__":
    run()
