from playwright.sync_api import sync_playwright, TimeoutError
import time
import re
from collections import Counter, defaultdict

# Allowed stamp database domains
ALLOWED_DOMAINS = ["colnect", "mysticstamp", "hipstamp"]
SEARCH_TEXT = "scott SC SN # number"
SCOTT_REGEX = re.compile(r'''
    (?i)                                # case-insensitive
    \b                                   # word boundary
    (?:                                  # optional prefix somewhere before
        (?:(?:Scott|SC|SN)\s*\#?)\s*    # e.g., Scott#, SC#, SN#
    )?
    ([A-Z]?\d{1,4}[A-Z]?)               # capture Scott number
    \b
''', re.VERBOSE)

EXCLUDE_REGEX = re.compile(r'''
    ^(?:                  
        \d+[./]\d+|        # fractions or decimals
        \d+(?:st|nd|rd|th)| # ordinals
        [€£$¢]\d+          # currency
    )$
''', re.VERBOSE)

YEAR_REGEX = re.compile(r'^(18|19|20)\d{2}$', re.IGNORECASE)
DENOM_REGEX = re.compile(r'^\d+(?:c|d|p|¢)$', re.IGNORECASE)

SCOTT_PREFIX_REGEX = re.compile(
    r'(Scott|Sc|SC|SN)\s*#?\s*',
    re.IGNORECASE
)


CATALOG_CONTEXT_REGEX = re.compile(
    r'\b(used|mint|perf|wmk|watermark|variety)\b',
    re.IGNORECASE
)

OTHER_CATALOG_REGEX = re.compile(
    r'\b(SG|Michel|Yvert|Stanley)\b',
    re.IGNORECASE
)
scores = defaultdict(int)


class GoogleLensController:
    def __init__(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled"
            ]
        )
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.goto("https://lens.google.com/", timeout=60000)

    def search(self, image_path):
        try:
            self.page.goto("https://lens.google.com/", timeout=60000)

            # Upload the image
            file_input = self.page.locator("input[type='file']:not([hidden])")
            file_input.set_input_files(image_path)

            # Wait for network idle after upload
            self.page.wait_for_load_state("networkidle")
            time.sleep(2)

            # # Add additional text from global variable if defined
            # if SEARCH_TEXT:
            #     text_input = self.page.locator(
            #         "textarea[placeholder='Add to your search']"
            #     )

            #     if text_input.count() == 0:
            #         print("Add-to-search input not found — continuing without text refinement.")
            #     else:
            #         text_input.fill(SEARCH_TEXT)
            #         text_input.press("Enter")

            #         self.page.wait_for_load_state("networkidle")
            #         time.sleep(2)


            return self._scrape_visual_matches()

        except TimeoutError:
            print("Lens search failed: file input not found or timed out")
            return []

    def _scrape_visual_matches(self):
        results = []

        try:
            # Wait until any images inside jsslot load
            self.page.wait_for_selector("div[jsslot] img", timeout=15000)
        except Exception:
            return results

        containers = self.page.query_selector_all("div[id=search] div:has(div[jsslot])")

        seen_srcs = set()

        for container in containers:
            try:
                #print ("CONTAINER: ", container, "\n\n")
                images = container.query_selector_all("img")
                for img in images:
                    html = img.evaluate("el => el.outerHTML")
                    #print("IMAGE: ", html, "\n\n")
                    src = img.get_attribute("src")
                    if not src or src in seen_srcs:
                        continue

                    # Skip tiny placeholders
                    width = img.evaluate("el => el.width")
                    height = img.evaluate("el => el.height")
                    if width < 50 or height < 50:
                        continue

                    seen_srcs.add(src)

                    # find site link
                    print("searching for site link...")
                    site_handle = img.query_selector("xpath=ancestor::a | following::a")
                    site_link = site_handle.get_attribute("href")
                    print ("SITE LINK: ", site_link)

                    if not any(site in site_link.lower() for site in ALLOWED_DOMAINS):
                        print("Skipping non-allowed domain:", site_link)
                        continue

                    # find page title
                    title_div_handle = img.query_selector("xpath=ancestor::div[@aria-label][1]")
                    title = title_div_handle.get_attribute("aria-label") if title_div_handle else ""
                    
                    # append result if it has passed filtering
                    results.append({
                        "thumbnail_url": src,
                        "link": site_link,
                        "title": title or img.get_attribute("alt") or "",
                    })

                    if len(results) >= 8:
                        return results

            except Exception as e:
                print("Error processing container")
                print (e)
                continue

        return results
    
    def filter_results_by_domain(self, results):
        filtered = []
        for item in results:
            link = item.get("link", "")
            if any(domain in link for domain in ALLOWED_DOMAINS):
                filtered.append(item)
        return filtered
    def extract_scott_numbers(self, results):
        # Store list of scores per candidate
        candidate_scores = defaultdict(list)

        for item in results:
            title = item.get("title", "")
            matches = SCOTT_REGEX.finditer(title)

            for match in matches:
                candidate = match.group(1)
                score = 0

                # ---- Positive signals ----

                # Explicit Scott prefix near this match
                prefix_pattern = SCOTT_PREFIX_REGEX.pattern.format(re.escape(candidate))
                if re.search(prefix_pattern, title):
                    score += 5

                # Letter suffix or prefix (555Q, 12a, C4)
                if re.match(r'^[A-Z]\d', candidate) or re.search(r'[A-Z]$', candidate, re.IGNORECASE):
                    score += 2

                # Standalone #number is a weak Scott signal
                if re.search(rf'\#{candidate}\b', title):
                    score += 3

                # Catalog-style descriptive context
                if CATALOG_CONTEXT_REGEX.search(title):
                    score += 1

                # Other catalog references nearby
                if OTHER_CATALOG_REGEX.search(title):
                    score += 1

                # ---- Negative signals ----

                # Year-like values
                if YEAR_REGEX.match(candidate):
                    score -= 3

                # Denomination-like values
                if DENOM_REGEX.match(candidate):
                    score -= 4

                candidate_scores[candidate].append(score)

                print(f"Candidate: {candidate} | Match score: {score}")
                print("Title:", title, "\n")

        if not candidate_scores:
            return None

        # Aggregate score = sum of per-match scores
        aggregated = {
            candidate: sum(scores)
            for candidate, scores in candidate_scores.items()
        }

        # Optional debug output
        print("Aggregated Scott candidate scores:")
        for c, s in aggregated.items():
            print(f"  {c}: {s}")

        # Select highest-confidence candidate
        return max(aggregated.items(), key=lambda x: x[1])[0]



    def close(self):
        self.context.close()
        self.browser.close()
        self.playwright.stop()
