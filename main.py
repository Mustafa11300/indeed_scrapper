from playwright.sync_api import sync_playwright
import json
import os
import time
import random

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
CANDIDATES_FILE = "data/extracted_candidates.json"
STATE_FILE      = "data/scrape_state.json"
SESSION_DIR     = "./indeed_session"

# *** THE MAIN SPEED SWITCH ***
# Watch the console on the FIRST page load. If you already see "[+] Extracted"
# lines with real phone numbers BEFORE any clicking happens, the phones come
# from the list view -> set this to False. The script will then just paginate
# and let the network handler collect everyone. This is many times faster.
#
# Keep it True only if phone numbers ONLY appear when you open a candidate.
CLICK_CANDIDATES = True

# Used only for the "start from candidate number" option. Leave as None to
# auto-detect from page 1. If detection looks wrong (e.g. virtualization only
# renders some rows), set the real number of candidates per page here.
CANDIDATES_PER_PAGE = None

os.makedirs("data", exist_ok=True)

scraped_candidates = []
seen_phones = set()

# ---------------------------------------------------------------------------
# STATE / PERSISTENCE
# ---------------------------------------------------------------------------
def load_candidates():
    """Load any previously scraped candidates so resume never creates dupes."""
    global scraped_candidates, seen_phones
    if os.path.exists(CANDIDATES_FILE):
        try:
            with open(CANDIDATES_FILE, "r", encoding="utf-8") as f:
                scraped_candidates = json.load(f)
            seen_phones = {c["phone"] for c in scraped_candidates}
            print(f"Loaded {len(scraped_candidates)} existing candidates.")
        except Exception:
            scraped_candidates = []
            seen_phones = set()

def save_candidates():
    with open(CANDIDATES_FILE, "w", encoding="utf-8") as f:
        json.dump(scraped_candidates, f, indent=4, ensure_ascii=False)

def load_last_page():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("last_completed_page", 0)
        except Exception:
            return 0
    return 0

def save_last_page(page_num):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_completed_page": page_num}, f)

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def human_delay(min_sec=0.2, max_sec=0.5):
    """Small jitter for anti-bot. Kept tiny -- the real waits are event-based."""
    time.sleep(random.uniform(min_sec, max_sec))

def handle_response(response):
    if "apis.indeed.com/graphql" in response.url and response.status == 200:
        try:
            json_data = response.json()
        except Exception:
            return
        data = json_data.get("data") or {}
        subs = data.get("candidateSubmissions") or {}
        results = subs.get("results", [])
        added = False
        for candidate in results:
            profile = candidate.get("data", {}).get("profile", {})
            name  = profile.get("name", {}).get("displayName", "No Name")
            phone = profile.get("contact", {}).get("phoneNumber", "No Phone")
            if phone and phone != "No Phone" and phone not in seen_phones:
                seen_phones.add(phone)
                scraped_candidates.append({"name": name, "phone": phone})
                print(f"[+] Extracted: {name} - {phone}")
                added = True
        if added:
            save_candidates()  # cheap crash-safety; file is tiny

def wait_for_list(page):
    """Event-based waiting instead of blind sleeps -- this is the lag fix."""
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_selector("tbody tr, [role='row']", state="visible", timeout=30000)

def is_header_row(text):
    t = text.lower()
    return "matches to job post" in t or "candidates" in t

def count_candidate_rows(page):
    """Count non-header candidate rows on the current page."""
    rows = page.locator("tbody tr, [role='row']")
    n = rows.count()
    c = 0
    for i in range(n):
        try:
            if not is_header_row(rows.nth(i).inner_text()):
                c += 1
        except Exception:
            pass
    return c

def get_next_button(page):
    sel = ("button[aria-label*='Next' i], a[aria-label*='Next' i], "
           "button:has-text('Next'), a:has-text('Next')")
    return page.locator(sel).last

def go_to_next_page(page):
    """Advance one page. Returns False if no enabled Next button (end of list)."""
    btn = get_next_button(page)
    if btn.count() == 0:
        return False
    try:
        btn.scroll_into_view_if_needed()
    except Exception:
        pass
    if not (btn.is_enabled() and btn.is_visible()):
        return False
    human_delay()
    btn.click(force=True)
    wait_for_list(page)
    return True

def process_current_page(page, skip_count=0):
    """Open each candidate (in click mode) to trigger its detail network call.
    skip_count skips the first N candidates on THIS page (used when starting
    from a specific candidate number). Does nothing in list mode."""
    if not CLICK_CANDIDATES:
        human_delay(0.3, 0.6)
        return

    count = page.locator("tbody tr, [role='row']").count()
    seen_candidates = 0
    for i in range(count):
        try:
            row = page.locator("tbody tr, [role='row']").nth(i)
            if is_header_row(row.inner_text()):
                continue
            seen_candidates += 1
            if seen_candidates <= skip_count:
                continue  # skip candidates before the requested start number
            row.click(position={"x": 150, "y": 30}, force=True)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.go_back()
            wait_for_list(page)
        except Exception:
            # Dedup by phone makes a re-processed row harmless.
            pass

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def run(intent):
    """intent is ('page', n)  or  ('candidate', n)."""
    with sync_playwright() as p:
        print("Launching browser context...")
        browser = p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.on("response", handle_response)

        print("Navigating to Indeed...")
        page.goto("https://employers.indeed.com/candidates")
        print("\n*** WAITING FOR PAGE LOAD ***")
        wait_for_list(page)

        current_page = 1
        skip_in_page = 0

        # ---- Work out where to start ----
        if intent[0] == "candidate":
            target_candidate = intent[1]
            if not CLICK_CANDIDATES:
                print("NOTE: CLICK_CANDIDATES is False, so the network handler will still "
                      "capture candidates on pages you skip past. The candidate-number "
                      "start only truly limits scraping when CLICK_CANDIDATES is True.")
            page_size = CANDIDATES_PER_PAGE or count_candidate_rows(page)
            if page_size <= 0:
                page_size = 25
                print("Could not detect candidates per page; assuming 25. "
                      "Set CANDIDATES_PER_PAGE if that's wrong.")
            else:
                print(f"Detected ~{page_size} candidates per page.")
            start_page   = (target_candidate - 1) // page_size + 1
            skip_in_page = (target_candidate - 1) % page_size
            print(f"Candidate {target_candidate} -> page {start_page}, "
                  f"skipping first {skip_in_page} on that page.")
        else:
            start_page = intent[1]

        # ---- Fast-forward to the start page ----
        if start_page > 1:
            print(f"\nSkipping forward to page {start_page}...")
            while current_page < start_page:
                if not go_to_next_page(page):
                    print("Reached the end before the target page. Starting here.")
                    skip_in_page = 0
                    break
                current_page += 1
            print(f"Now on page {current_page}.")

        # ---- Main scrape loop ----
        while True:
            print(f"\n=== Processing page {current_page} ===")
            process_current_page(page, skip_count=skip_in_page)
            skip_in_page = 0                 # only the landing page skips
            save_last_page(current_page)     # mark page done AFTER processing
            save_candidates()

            print("Looking for 'Next' page...")
            if go_to_next_page(page):
                current_page += 1
            else:
                print("\nFinished! No more pages.")
                break

        save_candidates()
        print(f"\nDone. Saved {len(scraped_candidates)} unique candidates to {CANDIDATES_FILE}.")
        browser.close()

def ask_start_mode():
    """Returns an intent tuple: ('page', n) or ('candidate', n)."""
    last = load_last_page()
    print("\nHow do you want to start?")
    if last > 0:
        print(f"  [R] Resume from where you left off   (page {last + 1})")
    print(f"  [S] Start from the beginning          (page 1)")
    print(f"  [N] Start from a specific candidate number (e.g. the 200th)")
    choice = input("Choose R / S / N: ").strip().lower()

    if choice == "r" and last > 0:
        print(f"Resuming at page {last + 1}.")
        return ("page", last + 1)

    if choice == "n":
        while True:
            raw = input("Start from which candidate number? (e.g. 200): ").strip()
            try:
                num = int(raw)
                if num >= 1:
                    return ("candidate", num)
            except ValueError:
                pass
            print("Please enter a whole number of 1 or more.")

    # default: start fresh
    save_last_page(0)
    print("Starting fresh from page 1 (already-collected phones are kept and skipped).")
    return ("page", 1)

if __name__ == "__main__":
    load_candidates()
    run(ask_start_mode())