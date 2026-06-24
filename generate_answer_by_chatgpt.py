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

COOKIES_DIR = Path("chatgpt_cookies")
encrypted_files = list(COOKIES_DIR.glob("*.encrypted"))

if not encrypted_files:
    raise RuntimeError("❌ No .encrypted cookie files found in 'chatgpt_cookies/' folder")

CHATGPT_COOKIES_FILE = random.choice(encrypted_files)
print(f"[OK] Randomly selected cookie file: {CHATGPT_COOKIES_FILE.name}", flush=True)

PBKDF2_ITERATIONS = 200_000

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


# =========================
# ENV
# =========================
load_dotenv()

DECRYPT_KEY = os.getenv("DECRYPT_KEY")

if not DECRYPT_KEY:
    raise RuntimeError("DECRYPT_KEY missing")


# =========================
# RANDOM WAIT
# =========================
def custom_random_wait(min_sec, max_sec):
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

    # normalize SameSite and PartitionKey
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
# MAIN
# =========================
def run():
    print("[START] Script started", flush=True)

    # =========================
    # STATUS CHECK
    # =========================
    status_file = Path("status.json")
    if not status_file.exists():
        print("[ERROR] status.json file nahi mila. Exiting...", flush=True)
        sys.exit(0)
        
    try:
        with status_file.open("r", encoding="utf-8") as f:
            status_data = json.load(f)
    except Exception as e:
        print(f"[ERROR] status.json parse nahi ho paya: {e}. Exiting...", flush=True)
        sys.exit(0)

    post_found = status_data.get("post_to_answer_found", False)
    answer_gen = status_data.get("answer_generated", False)

    # Condition Check (Pylance typo fixed here)
    if post_found is True and answer_gen is False:
        print("[OK] Status check passed (post_to_answer_found is True & answer_generated is False). Proceeding...", flush=True)
    else:
        if post_found is False:
            print("Answer Not Generated Yet!", flush=True)
        elif answer_gen is True:
            print("Answer already generated!", flush=True)
        sys.exit(0)

    # Target content extract karna prompt ke liye
    post_content = status_data.get("content_of_post_to_answer", "")
    if not post_content:
        print("[ERROR] content_of_post_to_answer khali hai. Exiting...", flush=True)
        sys.exit(0)

    cookies = load_cookies(Path(CHATGPT_COOKIES_FILE))
    print(f"[OK] Total cookies loaded: {len(cookies)}", flush=True)

    # =========================
    # STEALTH SETUP & LOGIN
    # =========================
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
        print("[STEP] Adding cookies to browser context...", flush=True)
        context.add_cookies(cookies)

        page = context.new_page()
        print("[OK] Cookies added successfully", flush=True)

        print("[STEP] Opening ChatGPT Main URL...", flush=True)
        page.goto(
            "https://chatgpt.com/",
            wait_until="load"
        )
        print("[OK] URL opened successfully (Logged In)", flush=True)

        # 15 to 30 seconds random wait after page load
        custom_random_wait(30, 60)

        # ============================================
        # CHECK LOGIN SUCCESS VIA USER PROFILE BUTTON
        # ============================================
        print("[STEP] Checking login success via profile button...", flush=True)
        profile_button = page.get_by_role('button', name=list(map(lambda x: x.compile(r'.*Free, open'), [__import__('re')]))[0])
        
        if profile_button.count() > 0:
            print(f"[OK] LOGIN SUCCESS: Profile button found -> '{profile_button.first.get_attribute('aria-label') or 'User Account'}'", flush=True)
        else:
            print("[WARNING] Profile button not detected directly, proceeding with caution...", flush=True)

        # =========================
        # AUTOMATION FLOW
        # =========================
        print("[STEP] Locating chat textbox...", flush=True)
        
        # Fallback Strategy for Textbox Locators
        textbox = page.get_by_role('textbox', name='Chat with ChatGPT')
        
        if textbox.count() == 0:
            print("[INFO] Fallback 1: Searching for 'Ask anything' paragraph inside textbox context...", flush=True)
            textbox = page.locator('div[contenteditable="true"]').filter(has=page.locator('p', has_text='Ask anything')).first
            
        if textbox.count() == 0:
            print("[INFO] Fallback 2: Searching via CSS Selector '#prompt-textarea'...", flush=True)
            textbox = page.locator('#prompt-textarea')

        # Trigger action if found
        if textbox.count() > 0:
            textbox.first.click()
            print("[OK] Textbox located and clicked successfully.", flush=True)
        else:
            raise RuntimeError("❌ Textbox locator load nahi ho paya (All strategies failed).")
            
        custom_random_wait(15, 30)

        # ============================================
        # PROMPT FOR QUORA NOTES (WITH 50-150 CHAR LIMIT)
        # ============================================
        STATE_FILE = "quora_cta_state.json"
        TARGET_RATIO = 0.35
        LOWER_BOUND = 0.20   # agar ratio isse neeche chala jaaye, force-correct karo
        UPPER_BOUND = 0.50   # agar ratio isse zyada ho jaaye, force-correct karo

        def load_state():
            if os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE, "r") as f:
                        content = f.read().strip()
                        if not content:
                            return {"total_answers": 0, "cta_count": 0}
                        return json.loads(content)
                except (json.JSONDecodeError, ValueError):
                    # file corrupted ya invalid JSON — safe default pe wapas chale jao
                    return {"total_answers": 0, "cta_count": 0}
            return {"total_answers": 0, "cta_count": 0}

        def save_state(state):
            with open(STATE_FILE, "w") as f:
                json.dump(state, f)

        state = load_state()
        current_ratio = (
            state["cta_count"] / state["total_answers"] if state["total_answers"] > 0 else TARGET_RATIO
        )

        if current_ratio < LOWER_BOUND:
            # bahut neeche chala gaya, definitely is baar CTA daal do
            include_profile_mention = True
        elif current_ratio > UPPER_BOUND:
            # bahut zyada ho gaya, is baar definitely skip karo
            include_profile_mention = False
        else:
            # comfortable range mein ho, normal randomness chalne do
            include_profile_mention = random.random() < TARGET_RATIO

        profile_promotion_block = (
            f"PROFILE PROMOTION RULE (MANDATORY FOR THIS ANSWER — STRICT):\n"
            f"The final answer MUST include exactly one natural, organic mention pointing the reader to check the author's Quora profile for more writing on this topic.\n"
            f"This is non-negotiable for this particular answer — it must be included.\n\n"
            f"It must feel like something a real person would casually add, not like a promotional CTA or ad.\n"
            f"It should feel like an afterthought or a natural extension of the insight just shared — not a separate pitch bolted onto the end.\n\n"
            f"Do NOT:\n"
            f"- Mention Gumroad, ebooks, selling, products, links, or money\n"
            f"- Use salesy phrasing like 'check out my profile for more' in a generic/templated way\n"
            f"- Make it sound like self-promotion or marketing\n"
            f"- Place it awkwardly disconnected from the insight\n\n"
            f"Instead, blend it as a natural continuation, e.g. in the spirit of (do not copy verbatim, vary the phrasing each time):\n"
            f"- 'I've written more on this exact pattern on my profile, if you want to go deeper.'\n"
            f"- 'There's a longer breakdown of this on my profile for anyone curious.'\n"
            f"- 'I go into this in more detail on my profile, in case it's useful.'\n\n"
            f"Vary the wording every time so it never feels copy-pasted or repetitive across answers.\n"
            f"This mention should ideally come right after the core insight, woven in naturally, not as a final disconnected sentence unless that placement genuinely feels organic.\n\n"
        ) if include_profile_mention else ""

        candidate_profile_line = (
            f"Every candidate must still satisfy the PROFILE PROMOTION RULE above.\n\n"
        ) if include_profile_mention else ""

        reader_value_profile_line = (
            f"- Does the profile mention feel naturally blended rather than tacked on?\n"
        ) if include_profile_mention else ""

        prompt = (
            f"STRICTLY CRITICALLY IMPORTANT: Your entire response must be wrapped in a single ```json code block. "
            f"Do not print any JSON outside of the code block. "
            f"Do not add any text, explanation, markdown, commentary, or notes before or after the code block.\n\n"

            f"Read the following Quora post carefully:\n"
            f"\"\"\"\n{post_content}\n\"\"\"\n\n"

            f"TASK:\n"
            f"Write a thoughtful, high-value Quora answer that contributes something genuinely useful to the discussion.\n\n"

            f"PRIMARY OBJECTIVE:\n"
            f"Write the kind of answer that thoughtful Quora readers naturally upvote because it helps them see the topic differently, more clearly, or more deeply.\n\n"

            f"DO NOT:\n"
            f"- Praise the author\n"
            f"- Congratulate the author\n"
            f"- Restate the post\n"
            f"- Give generic advice\n"
            f"- Sound motivational\n"
            f"- Use inspirational clichés\n"
            f"- Write filler content\n"
            f"- Chase originality for its own sake\n\n"

            f"DO:\n"
            f"- Add a meaningful perspective\n"
            f"- Extend the author's insight\n"
            f"- Introduce a useful distinction\n"
            f"- Reveal a hidden implication\n"
            f"- Surface a tradeoff, paradox, or overlooked truth\n"
            f"- Leave the reader with a memorable observation\n\n"

            f"CONTEXT FIT RULE:\n"
            f"The answer must feel like a direct response to THIS specific post.\n\n"

            f"Before writing, silently determine:\n"
            f"1. What experience, realization, struggle, or transformation the author is describing.\n"
            f"2. What deeper truth is hidden inside that experience.\n"
            f"3. What useful perspective most readers would miss.\n"
            f"4. What insight would naturally build on the author's observation.\n\n"

            f"A highly intelligent answer that ignores the author's actual experience is a weak answer.\n\n"

            f"The answer should feel incomplete or out of place if pasted under an unrelated post.\n\n"

            f"INSIGHT PRIORITY RULE:\n"
            f"Do not optimize for cleverness.\n\n"

            f"Prefer:\n"
            f"- Relevant insight\n"
            f"- Specific insight\n"
            f"- Practical insight\n"
            f"- Human observation\n\n"

            f"Over:\n"
            f"- Abstract philosophy\n"
            f"- Detached wisdom\n"
            f"- Intellectual showing off\n"
            f"- Generic self-improvement language\n\n"

            f"QUORA ENGAGEMENT PATTERN:\n"
            f"Strong Quora answers often do one or more of the following:\n"
            f"- Reframe the situation in a surprising but believable way\n"
            f"- Introduce a useful mental model\n"
            f"- Expose a hidden assumption\n"
            f"- Reveal a second-order consequence\n"
            f"- Turn a personal story into a broader insight\n"
            f"- Distinguish between two things people usually confuse\n\n"

            f"When appropriate, follow this invisible structure:\n"
            f"Observation → Reframe → Insight\n\n"

            f"TONE:\n"
            f"Write like a thoughtful, observant, articulate person.\n"
            f"Sound human.\n"
            f"Sound experienced.\n"
            f"Sound naturally intelligent.\n\n"

            f"Do NOT sound like:\n"
            f"- A life coach\n"
            f"- A therapist\n"
            f"- A motivational speaker\n"
            f"- A LinkedIn influencer\n"
            f"- A productivity guru\n"
            f"- A marketer\n"
            f"- An AI assistant\n\n"

            f"HUMANNESS RULE:\n"
            f"The answer should feel like a real person had an interesting thought after reading the post.\n\n"

            f"It should not feel generated from a template.\n\n"

            f"SPECIFICITY RULE:\n"
            f"The answer must directly engage with the core idea of the post.\n"
            f"Avoid writing something that could be pasted under hundreds of unrelated posts.\n\n"

            f"{profile_promotion_block}"

            f"LENGTH:\n"
            f"Preferred range: 250–500 characters including spaces.\n"
            f"The answer should be as short as possible, but as long as necessary to deliver a complete insight.\n"
            f"Do not compress a valuable idea merely to stay short.\n"
            f"A developed insight is preferred over a clever one-liner.\n"
            f"Use as many words as necessary to complete the thought.\n"
            f"Do not add filler.\n\n"

            f"FORMAT RULES:\n"
            f"- Single continuous line\n"
            f"- No newline characters\n"
            f"- No bullet points\n"
            f"- No markdown\n"
            f"- No emojis\n"
            f"- No hashtags\n"
            f"- No greetings\n"
            f"- No sign-offs\n\n"

            f"AVOID COMMON AI PHRASES:\n"
            f"- It's important to remember\n"
            f"- Ultimately\n"
            f"- Here's the thing\n"
            f"- Think of it this way\n"
            f"- The key takeaway\n"
            f"- Let's dive in\n"
            f"- On the one hand\n"
            f"- In today's world\n"
            f"- Or similar AI-sounding transitions\n\n"

            f"CANDIDATE GENERATION:\n"
            f"Silently generate at least 5 different candidate answers.\n\n"

            f"Generate:\n"
            f"- One insight-driven answer\n"
            f"- One emotional/introspective answer\n"
            f"- One practical answer\n"
            f"- One counter-intuitive answer\n"
            f"- One memorable/quotable answer\n\n"

            f"{candidate_profile_line}"

            f"READER VALUE TEST:\n"
            f"Before selecting the final answer, ask:\n"
            f"- Does this build on the author's actual experience?\n"
            f"- Does it contain a useful or memorable insight?\n"
            f"- Would a thoughtful reader learn something new?\n"
            f"- Would the author feel understood?\n"
            f"- Is there at least one sentence worth remembering?\n"
            f"{reader_value_profile_line}\n"

            f"MEMORABLE ENDING RULE:\n"
            f"Whenever natural, end with the strongest observation, distinction, or insight.\n"
            f"The final sentence should ideally be the sentence a reader would highlight, quote, remember, or repeat.\n"
            f"Do not force a punchline.\n"
            f"If two candidates are similarly strong, prefer the one with the more memorable ending.\n\n"

            f"Choose the strongest candidate.\n\n"

            f"OUTPUT FORMAT — strictly inside a single JSON code block:\n"
            f"{{\n"
            f'  \"answer\": \"Your final single-line Quora answer here\"\n'
            f"}}\n"
        )

        print("[STEP] Entering prompt into textbox...", flush=True)
        textbox.first.fill(prompt)
        custom_random_wait(15, 30)

        print("[STEP] Locating and clicking send button...", flush=True)
        send_button = page.get_by_test_id('send-button')
        send_button.click()
        
        # Initial wait taaki generation properly start ho sake
        custom_random_wait(30, 60)

        # ============================================
        # STABLE 15-SECOND POLLING LIVE STREAM CHECK
        # ============================================
        print("[STEP] Waiting for generated JSON code block to complete writing (15s checks)...", flush=True)
        code_block_locator = page.locator('#code-block-viewer pre')
        
        json_content = None
        for attempt in range(1, 6):
            print(f"[STEP] Checking code block locator (Attempt {attempt}/5)...", flush=True)
            
            if code_block_locator.count() > 0:
                print("[OK] Code block visible, parsing live text size variations...", flush=True)
                
                last_length = 0
                max_check_cycles = 15
                
                for cycle in range(max_check_cycles):
                    time.sleep(15)
                    
                    current_text = code_block_locator.first.or_(page.locator('#code-block-viewer pre')).inner_text().strip()
                    current_length = len(current_text)
                    
                    print(f"[STREAM INFO] Cycle {cycle+1}: Previous Length = {last_length}, Current Length = {current_length}", flush=True)
                    
                    if current_length > 0 and current_length == last_length:
                        if current_text.endswith("}"):
                            json_content = current_text
                            print("[OK] Content generation is fully finished and finalized.", flush=True)
                            break
                        else:
                            print("[WARNING] Text generation paused but JSON bracket '}' is missing. Waiting further...", flush=True)
                        
                    last_length = current_length
                
                if json_content:
                    break
            
            if attempt < 5:
                print(f"[WARNING] Code block completely write nahi hua ya block mila nahi. Next retry window...", flush=True)
                custom_random_wait(30, 60)
            else:
                print("❌ Max retries reached. Streaming complete nahi ho payi. Exiting script...", flush=True)
                try:
                    browser.close()
                except:
                    pass
                sys.exit(1)

        # JSON parsing, validation and Status Sync
        if json_content:
            try:
                print("[STEP] Parsing content as JSON...", flush=True)
                if json_content.startswith("```json"):
                    json_content = json_content.split("```json", 1)[1]
                if json_content.endswith("```"):
                    json_content = json_content.rsplit("```", 1)[0]
                
                parsed_json = json.loads(json_content.strip())
                generated_answer_text = parsed_json.get("answer", "").strip()

                # Double safety: Remove any stray newlines from string
                generated_answer_text = generated_answer_text.replace("\n", " ").replace("\r", "")
                
                # =====================================
                # UPDATE STATUS.JSON ONLY (NO TOPICS.TXT INTERACTION)
                # =====================================
                print("[STEP] Updating status.json with answer data...", flush=True)
                status_data["answer"] = generated_answer_text
                status_data["answer_generated"] = True
                
                with status_file.open("w", encoding="utf-8") as f:
                    json.dump(status_data, f, indent=4, ensure_ascii=False)
                print("[OK] status.json successfully updated (answer appended & answer_generated=True)", flush=True)
                
                state["total_answers"] += 1
                if include_profile_mention:
                    state["cta_count"] += 1
                save_state(state)
            except json.JSONDecodeError as je:
                print(f"[ERROR] Content JSON parse karne me fail hua: {je}. Exiting script...", flush=True)
                try:
                    browser.close()
                except:
                    pass
                sys.exit(1)
        else:
            print("[ERROR] Save skip kiya gaya kyunki koi data fetch nahi hua. Exiting script...", flush=True)
            try:
                browser.close()
            except:
                pass
            sys.exit(1)

        # 15 to 30 seconds random wait before closing the browser normally
        print("[STEP] Performing random wait before normal browser closure...", flush=True)
        custom_random_wait(15, 30)

    except SystemExit:
        raise
    except Exception as e:
        print("[ERROR]", e, flush=True)
        # CAPTURE SCREENSHOT ON ERROR
        if 'page' in locals() and page:
            try:
                screenshot_path = "error_screenshot.png"
                page.screenshot(path=screenshot_path, full_page=True)
                print(f"[OK] Error screenshot captured: {screenshot_path}", flush=True)
            except Exception as screenshot_err:
                print(f"[WARNING] Could not capture screenshot: {screenshot_err}", flush=True)
        if browser:
            try:
                browser.close()
            except:
                pass
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

        print("[DONE] Script finished", flush=True)


if __name__ == "__main__":
    run()