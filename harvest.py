from playwright.sync_api import sync_playwright
import json
import csv
import os
import re
import sys

os.makedirs("data", exist_ok=True)
JSON_FILE = "data/candidates.json"
CSV_FILE  = "data/candidates.csv"
SESSION_DIR = "./indeed_session"
START_URL = "https://employers.indeed.com/candidates?statusName=All&tab=manage&id=0"

# Note: email (aliasedEmail) only appears in the per-candidate DETAIL page
# response, never in the list/grid view. This script intentionally stays on
# the list view and does not crawl detail pages, so email will stay blank
# for everyone -- that's expected, not a bug, per your call to skip it.

SORT_PASSES = ["newest", "oldest"]

# Status buckets, confirmed against the actual 'Status' dropdown in the UI:
# All applications 9012 | New 6114 | Reviewing 2898 | Contacting 0 |
# Interviewing 0 | Rejected 18 | Hired 0
# (Counts don't sum exactly to the All-applications total -- buckets overlap
# rather than strictly partition -- but each is still worth a pass since the
# 'All' view's pagination stalls before reaching everyone.)
STATUS_SEGMENTS = ["New", "Reviewing", "Contacting", "Interviewing", "Rejected", "Hired"]

candidates = {}   # submission_id -> record (ordered)
known_total = None  # true total pulled from findCandidateSubmissions.totalCount


def parse_totals(data):
    """Capture the true applicant total from the findCandidateSubmissions
    GraphQL response, e.g. {"data": {"findCandidateSubmissions":
    {"totalCount": 8994}}}. Far more reliable than scraping footer text."""
    global known_total
    root = (data.get("data") or {}).get("findCandidateSubmissions")
    if root and "totalCount" in root:
        known_total = root["totalCount"]
        return known_total
    return None


def load_existing(fresh=False):
    """Load previously saved candidates.json into memory, unless `fresh` is
    True, in which case we skip loading entirely and start from empty --
    the next save() call will overwrite the old file on disk."""
    if fresh:
        print("Fresh start requested -- ignoring any existing candidates.json "
              "(it will be overwritten once new data comes in).")
        return
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
            "name", "phone", "email", "location", "job_title", "created", "submission_id"])
        w.writeheader()
        w.writerows(rows)


def _extract_records(data):
    """Yield (sub_id, partial_record_dict) for every candidate submission found
    in a GraphQL response, regardless of which query/shape produced it.

    Two shapes have been observed in practice:
      1. data.findRCPMatches.matchConnection.matches[].candidateSubmission
      2. data.candidateSubmissions.results[]   <- the real shape seen live

    Each response may only carry a subset of fields (e.g. one call returns
    phone, another returns aliasedEmail), so callers must MERGE into existing
    records rather than skip when the id is already known.
    """
    payload = data.get("data") or {}

    # Shape 2 (confirmed live shape)
    root2 = payload.get("candidateSubmissions")
    if root2:
        for cs in root2.get("results", []):
            d = cs.get("data") or {}
            sub_id = d.get("submissionUuid") or cs.get("id")
            if not sub_id:
                continue
            profile = d.get("profile") or {}
            contact = profile.get("contact") or {}
            name = (profile.get("name") or {}).get("displayName")
            phone = contact.get("phoneNumber")
            email = (contact.get("aliasedEmail") or contact.get("email")
                     or contact.get("emailAddress"))
            loc = (profile.get("location") or {}).get("location")
            job_title = None
            meta = d.get("metadata") or []
            if meta:
                job_title = (meta[0].get("data") or {}).get("jobTitle")
            # job title can also live under job.node.jobData in this shape
            if not job_title:
                job_node = ((d.get("job") or {}).get("node") or {}).get("jobData") or {}
                job_title = job_node.get("title")
            yield sub_id, {
                "name": name, "phone": phone, "email": email,
                "location": loc, "job_title": job_title,
                "created": d.get("created"),
            }

    # Shape 1 (older/alternate shape, kept for compatibility)
    root1 = payload.get("findRCPMatches")
    if root1:
        conn = root1.get("matchConnection") or {}
        for m in conn.get("matches", []):
            cs = m.get("candidateSubmission") or {}
            d = cs.get("data") or {}
            sub_id = d.get("submissionUuid") or cs.get("id")
            if not sub_id:
                continue
            profile = d.get("profile") or {}
            contact = profile.get("contact") or {}
            name = (profile.get("name") or {}).get("displayName")
            phone = contact.get("phoneNumber")
            email = (contact.get("aliasedEmail") or contact.get("email")
                     or contact.get("emailAddress"))
            loc = (profile.get("location") or {}).get("location")
            job_title = None
            meta = d.get("metadata") or []
            if meta:
                job_title = (meta[0].get("data") or {}).get("jobTitle")
            yield sub_id, {
                "name": name, "phone": phone, "email": email,
                "location": loc, "job_title": job_title,
                "created": d.get("created"),
            }


def parse_response(data):
    added = 0
    updated = 0
    for sub_id, partial in _extract_records(data):
        existing = candidates.get(sub_id)
        if existing is None:
            candidates[sub_id] = {
                "name": partial.get("name") or "",
                "phone": partial.get("phone") or "",
                "email": partial.get("email") or "",
                "location": partial.get("location") or "",
                "job_title": partial.get("job_title") or "",
                "created": partial.get("created") or "",
                "submission_id": sub_id,
            }
            added += 1
            rec = candidates[sub_id]
            if rec["phone"]:
                email_note = f" - {rec['email']}" if rec["email"] else ""
                print(f"[+] {rec['name']} - {rec['phone']}{email_note}")
        else:
            changed = False
            for field in ("name", "phone", "email", "location", "job_title", "created"):
                val = partial.get(field)
                if val and not existing.get(field):
                    existing[field] = val
                    changed = True
            if changed:
                updated += 1
                print(f"[~] filled in fields for {existing['name']} "
                      f"- {existing['phone'] or '-'} - {existing['email'] or '-'}")
    return added + updated


def on_response(response):
    if "graphql" not in response.url or response.status != 200:
        return
    try:
        data = response.json()
    except Exception:
        return
    parse_totals(data)  # cheap check; no-op unless this is the totals query
    if parse_response(data) > 0:
        save()


def read_footer(page):
    """Return (start, end, total) from the pagination footer text.
    Tries several patterns/selectors since Indeed's wording/markup can vary
    ('Showing 1-50 of 3000', '1-50 of 3,000 results', etc.)."""
    patterns = [
        r"Showing\s+([\d,]+)\s*[-\u2013]\s*([\d,]+)\s+of\s+([\d,]+)",
        r"([\d,]+)\s*[-\u2013]\s*([\d,]+)\s+of\s+([\d,]+)",
    ]
    candidates_text = []
    # 1) try the specific 'Showing' text locator
    try:
        loc = page.locator("text=/Showing\\s+\\d/i").last
        if loc.count():
            candidates_text.append(loc.inner_text())
    except Exception:
        pass
    # 2) broaden: any element containing "of" + digits (pagination footers)
    try:
        loc2 = page.locator("text=/\\bof\\s+[\\d,]+/i")
        n = loc2.count()
        for i in range(min(n, 5)):
            try:
                candidates_text.append(loc2.nth(i).inner_text())
            except Exception:
                continue
    except Exception:
        pass

    for t in candidates_text:
        for pat in patterns:
            m = re.search(pat, t, re.I)
            if m:
                return (int(m.group(1).replace(",", "")),
                        int(m.group(2).replace(",", "")),
                        int(m.group(3).replace(",", "")))

    # 3) nothing matched -- print what we found so you can see the real markup,
    # and also check for an explicit empty-state message so callers can tell
    # "zero results" apart from "footer markup we don't recognize".
    if candidates_text:
        print("  [debug] footer text found but didn't match expected pattern:")
        for t in candidates_text[:5]:
            print(f"    -> {t!r}")
    else:
        try:
            empty_loc = page.locator(
                "text=/no candidates|no results|no applications found/i")
            if empty_loc.count():
                print("  [debug] page shows an explicit 'no results' message.")
            else:
                print("  [debug] no footer/pagination text located on page at all "
                      "(and no explicit empty-state message either).")
        except Exception:
            print("  [debug] no footer/pagination text located on page at all.")
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


def goto_status(page, status_name):
    """Navigate directly via URL query param to a specific status filter.
    Your START_URL already uses statusName=All, so this swaps that value."""
    from urllib.parse import quote
    url = (f"https://employers.indeed.com/candidates"
           f"?statusName={quote(status_name)}&tab=manage&id=0")
    page.goto(url)
    try:
        page.wait_for_selector("tbody tr, [role='row']", timeout=30000)
    except Exception:
        pass
    page.wait_for_timeout(2000)


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

        # Give the totals query a moment to fire and be captured by on_response.
        page.wait_for_timeout(1500)
        total = known_total
        if total is None:
            foot = read_footer(page)
            total = foot[2] if foot else None
            print(f"\nApplicant total (from footer, totalCount not seen yet): {total}")
        else:
            print(f"\nApplicant total (from findCandidateSubmissions.totalCount): {total}")

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

        total = known_total or total

        # If newest+oldest passes still leave a gap vs. the server-reported
        # total, try segmenting by status -- this only helps if Indeed's
        # 'All' view itself has a hidden pagination ceiling that a narrower
        # status filter (fewer results per bucket) can get under.
        if total and len(candidates) < total:
            gap = total - len(candidates)
            print(f"\n{gap} candidates still missing after newest+oldest passes.")
            print("Trying status-segmented passes to close the gap...")
            for status in STATUS_SEGMENTS:
                if len(candidates) >= total:
                    break
                print(f"\n===== PASS: status = {status!r} =====")
                try:
                    goto_status(page, status)
                except Exception as e:
                    print(f"  Skipping status {status!r}: {e}")
                    continue
                page.wait_for_timeout(1500)

                # Log the footer for visibility only -- do NOT skip based on
                # it. With small total counts (a few hundred), a status
                # legitimately matching the 'All' total is plausible (e.g.
                # nearly everyone sitting in one bucket), so the old
                # "matches All -> assume bad label -> skip" heuristic caused
                # false negatives. Always harvest; dedup makes re-scraping
                # already-known candidates cheap/harmless.
                foot_check = read_footer(page)
                if foot_check:
                    print(f"  Status filter reports {foot_check[2]} candidates.")
                else:
                    # No footer text usually means zero results for this
                    # filter, but confirm via the DOM row count rather than
                    # assuming -- this is what previously caused status
                    # passes to be silently skipped over.
                    try:
                        row_count = page.locator("tbody tr, [role='row']").count()
                    except Exception:
                        row_count = -1
                    print(f"  No footer text; DOM row count = {row_count}.")
                    if row_count == 0:
                        print(f"  '{status}' appears to have 0 candidates. Skipping.")
                        continue

                before_count = len(candidates)
                harvest_pages(page)
                after_count = len(candidates)
                print(f"  '{status}' pass added {after_count - before_count} new candidates "
                      f"(page reported {foot_check[2] if foot_check else 'unknown'} total).")
                save()

        print(f"\nDone. {len(candidates)} candidates -> {JSON_FILE} and {CSV_FILE}")
        if total:
            print(f"Server-reported total: {total}")
        if total and len(candidates) < total:
            print(f"NOTE: still missing {total - len(candidates)} candidates after "
                  "newest/oldest/status passes. This likely means Indeed caps "
                  "pagination depth on the 'All' view (e.g. won't page past "
                  "~9000 regardless of sort order), and STATUS_SEGMENTS above "
                  "doesn't exactly match your account's actual status filter "
                  "options. Check the 'Status' dropdown in the browser and "
                  "update STATUS_SEGMENTS to match what you see there, or tell "
                  "me the exact labels and I'll fix the list.")
        browser.close()


if __name__ == "__main__":
    # Pass --fresh on the command line to ignore any existing
    # data/candidates.json and start collecting from scratch, e.g.:
    #   python indeed_scraper.py --fresh
    fresh = "--fresh" in sys.argv
    load_existing(fresh=fresh)
    run()