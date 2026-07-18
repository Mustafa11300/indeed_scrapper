from playwright.sync_api import sync_playwright
import json
import os
import time
import random

os.makedirs("data", exist_ok=True)
scraped_candidates = []

def human_delay(min_sec=1.0, max_sec=2.0):
    """Adds a randomized delay to mimic human behavior and allow DOM rendering."""
    time.sleep(random.uniform(min_sec, max_sec))

def handle_response(response):
    if "apis.indeed.com/graphql" in response.url and response.status == 200:
        try:
            json_data = response.json()
            if "data" in json_data and "candidateSubmissions" in json_data["data"]:
                results = json_data["data"]["candidateSubmissions"].get("results", [])
                
                for candidate in results:
                    candidate_data = candidate.get("data", {})
                    profile = candidate_data.get("profile", {})
                    
                    name_dict = profile.get("name", {})
                    name = name_dict.get("displayName", "No Name")
                    
                    contact_dict = profile.get("contact", {})
                    phone = contact_dict.get("phoneNumber", "No Phone")
                    
                    if phone != "No Phone":
                        if not any(c['phone'] == phone for c in scraped_candidates):
                            print(f"[+] Extracted: {name} - {phone}")
                            scraped_candidates.append({"name": name, "phone": phone})
                            
                            with open("data/extracted_candidates.json", "w", encoding="utf-8") as f:
                                json.dump(scraped_candidates, f, indent=4)
        except Exception:
            pass

def run():
    with sync_playwright() as p:
        print("Launching browser context...")
        browser = p.chromium.launch_persistent_context(
            user_data_dir="./indeed_session", 
            headless=False, 
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = browser.pages[0]
        page.on("response", handle_response)
        
        print("Navigating to Indeed...")
        page.goto("https://employers.indeed.com/candidates") 
        
        print("\n*** WAITING FOR PAGE LOAD ***")
        page.wait_for_selector("tbody tr, [role='row']", timeout=60000)
        human_delay(1.5, 2.5)
        
        print("\n*** STARTING AUTOMATED EXTRACTION ***")
        
        clicked_rows = 0
        
        while True:
            rows = page.locator("tbody tr, [role='row']")
            count = rows.count()
            
            if clicked_rows >= count:
                print(f"--> Finished processing current page. Looking for 'Next' button...")
                
                next_selector = "button[aria-label*='Next' i], a[aria-label*='Next' i], button:has-text('Next'), a:has-text('Next')"
                
                try:
                    page.wait_for_selector(next_selector, state="attached", timeout=5000)
                except Exception:
                    pass
                
                next_button = page.locator(next_selector).last
                
                if next_button.count() > 0:
                    next_button.scroll_into_view_if_needed()
                    human_delay(0.5, .75)
                    
                    if next_button.is_enabled() and next_button.is_visible():
                        print("Clicking Next Page...")
                        human_delay(0.5, .75)
                        next_button.click(force=True)
                        
                        print("Waiting for new candidates to load...")
                        human_delay(2.0, 3.5)
                        page.wait_for_selector("tbody tr, [role='row']", state="visible", timeout=15000)
                        
                        clicked_rows = 0
                        continue
                    else:
                        print("\nFinished! 'Next' button is disabled. Reached the last page.")
                        break
                else:
                    print("\nFinished! Could not find a 'Next' button. Reached the end of the list.")
                    break
            
            for i in range(clicked_rows, count):
                try:

                    row = page.locator("tbody tr, [role='row']").nth(i)
                    
                    row_text = row.inner_text().lower()
                    if "matches to job post" in row_text or "candidates" in row_text:
                        clicked_rows += 1
                        continue
                    
                    row.scroll_into_view_if_needed()
                    human_delay(0.5, 1.0)
                    
                    row.click(position={"x": 150, "y": 30}, force=True)
                    
                    human_delay(1.0, 2.0)
                    
                    page.go_back()
                    
                    human_delay(1.5, 2.5)
                    page.wait_for_selector("tbody tr, [role='row']", state="visible", timeout=10000)
                    
                except Exception as e:
                    pass
                
                clicked_rows += 1

        print("\nBrowser closed. Extraction complete.")
        print(f"Success! Saved {len(scraped_candidates)} unique candidates to data/extracted_candidates.json.")

if __name__ == "__main__":
    run()