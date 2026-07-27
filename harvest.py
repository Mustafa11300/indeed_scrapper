from playwright.sync_api import sync_playwright
import json
import csv
import os
import re

os.makedirs("data", exist_ok=True)
JSON_FILE = "data/candidates.json"
CSV_FILE  = "data/candidates.csv"
SESSION_DIR = "./indeed_session"
START_URL = "https://employers.indeed.com/candidates?statusName=All&tab=manage&id=0"


SORT_PASSES = ["newest", "oldest"]

candidates = {}   # submission_id -> record (ordered)

def load_existing():
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                for rec in json.load(f):
                    key = rec.get("submission_id") or rec.get("phone")
                    if key:
                        candidates[key] = rec
            print(f"Loaded {len(candidates)} existing candidates (will resume/dedup).")
        except Exception:
            pass

def save():
    rows = list(candidates.values())
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    with open(CSV_FILE, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "name", "phone", "location", "job_title", "created", "submission_id"])
        w.writeheader()
        w.writerows(rows)


def parse_response(data):
    root = (data.get("data") or {}).get("findRCPMatches")
    if not root:
        return 0
    conn = root.get("matchConnection") or {}
    added = 0
    for m in conn.get("matches", []):
        cs = m.get("candidateSubmission") or {}
        d = cs.get("data") or {}
        profile = d.get("profile") or {}
        name  = (profile.get("name") or {}).get("displayName")
        phone = (profile.get("contact") or {}).get("phoneNumber")
        loc   = (profile.get("location") or {}).get("location")
        job_title = None
        meta = d.get("metadata") or []
        if meta:
            job_title = (meta[0].get("data") or {}).get("jobTitle")
        sub_id = d.get("submissionUuid") or cs.get("id")
        if not sub_id or sub_id in candidates:
            continue
        candidates[sub_id] = {
            "name": name or "", "phone": phone or "", "location": loc or "",
            "job_title": job_title or "", "created": d.get("created") or "",
            "submission_id": sub_id,
        }
        added += 1
        if phone:
            print(f"[+] {name} - {phone}")
    return added

def on_response(response):
    if "graphql" not in response.url or response.status != 200:
        return
    try:
        data = response.json()
    except Exception:
        return
    if parse_response(data) > 0:
        save()

def read_footer(page):
    """Return (start, end, total) from 'Showing A-B of TOTAL', else None."""
    try:
        loc = page.locator("text=/Showing\\s+\\d/i").last
        if loc.count():
            t = loc.inner_text()
            m = re.search(r"Showing\s+([\d,]+)\s*[-\u2013]\s*([\d,]+)\s+of\s+([\d,]+)",
                          t, re.I)
            if m:
                return (int(m.group(1).replace(",", "")),
                        int(m.group(2).replace(",", "")),
                        int(m.group(3).replace(",", "")))
    except Exception:
        pass
    return None

def open_sort_menu(page):
    for sel in ("button:has-text('Sort by')", "text=/Sort by/i",
                "[aria-label*='Sort' i]"):
        loc = page.locator(sel).last
        try:
            if loc.count():
                loc.click()
                page.wait_for_timeout(700)
                return True
        except Exception:
            continue
    return False

def set_sort(page, which):
    """Pick 'newest' or 'oldest first' from the Sort dropdown. Returns True on
    success. The list reloads to page 1 in the new order."""
    key = "oldest" if which == "oldest" else "newest"
    if not open_sort_menu(page):
        print("  Could not open the Sort menu.")
        return False
    for finder in (
        lambda: page.get_by_role("option", name=re.compile(key, re.I)),
        lambda: page.get_by_role("menuitemradio", name=re.compile(key, re.I)),
        lambda: page.get_by_role("menuitem", name=re.compile(key, re.I)),
        lambda: page.locator(f"[role='option']:has-text('{key}')"),
        lambda: page.locator(f"li:has-text('{key}')"),
    ):
        try:
            loc = finder()
            if loc.count():
                loc.last.click()
                page.wait_for_timeout(3000)  # list reloads
                print(f"  Sort set to '{which} first'.")
                return True
        except Exception:
            continue
    print(f"  Couldn't find the '{key} first' option automatically.")
    return False

def click_next(page):
    for sel in ("a:has-text('Next')", "button:has-text('Next')",
                "[aria-label*='Next' i]"):
        btn = page.locator(sel).last
        try:
            if btn.count() and btn.is_visible() and btn.is_enabled():
                btn.scroll_into_view_if_needed()
                btn.click()
                return True
        except Exception:
            continue
    return False

def harvest_pages(page):
    """Page through with Next until the cap / last page for the CURRENT sort."""
    stall = 0
    while True:
        foot = read_footer(page)
        pos = f"page shows {foot[0]}-{foot[1]} of {foot[2]}" if foot else ""
        print(f"Collected {len(candidates)}  {pos}")

        if foot and foot[1] >= foot[2]:
            print("  Reached the last page.")
            break

        before = len(candidates)
        if not click_next(page):
            print("  Next disabled -> cap reached for this sort order.")
            break

        waited = 0
        while len(candidates) == before and waited < 12000:
            page.wait_for_timeout(500)
            waited += 500

        if len(candidates) == before:
            stall += 1
            print(f"  No new candidates after Next (stall {stall}/3).")
            if stall >= 3:
                break
        else:
            stall = 0

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR, headless=False,
            args=["--disable-blink-features=AutomationControlled"])
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.on("response", on_response)

        print("Opening candidate list... log in if prompted.")
        page.goto(START_URL)
        try:
            page.wait_for_selector("tbody tr, [role='row']", timeout=60000)
        except Exception:
            print("Rows not detected -- log in if you see a login screen.")
        page.wait_for_timeout(4000)

        foot = read_footer(page)
        total = foot[2] if foot else None
        print(f"\nApplicant total (from footer): {total}")

        for i, which in enumerate(SORT_PASSES):
            print(f"\n===== PASS: sort {which} first =====")
            # 'newest' is the default order, so only actively change when needed.
            if which != "newest" or i > 0:
                if not set_sort(page, which):
                    print(f"  >>> Please set the 'Sort by' dropdown to "
                          f"'{which} first' in the browser NOW. Waiting 40s...")
                    for _ in range(20):
                        page.wait_for_timeout(2000)
            page.wait_for_timeout(2000)
            harvest_pages(page)
            save()

        print(f"\nDone. {len(candidates)} candidates -> {JSON_FILE} and {CSV_FILE}")
        if total and len(candidates) < total * 0.9:
            print(f"NOTE: collected {len(candidates)} of ~{total}. If both passes "
                  "capped at 3000 and the overlap wasn't enough, tell me the numbers "
                  "and we'll segment by another filter (location/status).")
        browser.close()

if __name__ == "__main__":
    load_existing()
    run()