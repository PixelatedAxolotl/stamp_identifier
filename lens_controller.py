import asyncio
import re
from collections import Counter, defaultdict
from playwright.async_api import async_playwright, TimeoutError
from config import LENS_ALLOWED_DOMAINS, LENS_HEADLESS
import pycountry

# Build a country-name lookup at import time.
# Keyed by lowercase name → canonical display name.
# Sorted longest-first so multi-word names are matched before substrings.
_COUNTRY_NAMES: dict[str, str] = {}

for _c in pycountry.countries:
    _COUNTRY_NAMES[_c.name.lower()] = _c.name
    if hasattr(_c, 'common_name'):
        _COUNTRY_NAMES[_c.common_name.lower()] = _c.name
    if hasattr(_c, 'official_name'):
        _COUNTRY_NAMES[_c.official_name.lower()] = _c.name

# Common aliases / shorthands not covered by pycountry
_COUNTRY_ALIASES = {
    "united states":  "United States of America",
    "great britain":  "United Kingdom",
    "britain":        "United Kingdom",
    "russia":         "Russia",
    "south korea":    "Korea, Republic of",
    "north korea":    "Korea, Democratic People's Republic of",
    "iran":           "Iran, Islamic Republic of",
    "vietnam":        "Viet Nam",
    "syria":          "Syrian Arab Republic",
    "tanzania":       "Tanzania, United Republic of",
    "bolivia":        "Bolivia (Plurinational State of)",
}
for _alias, _canonical in _COUNTRY_ALIASES.items():
    _COUNTRY_NAMES[_alias.lower()] = _canonical

# Sorted longest-first for greedy matching
_SORTED_NAMES = sorted(_COUNTRY_NAMES.keys(), key=len, reverse=True)

ALLOWED_DOMAINS = LENS_ALLOWED_DOMAINS
SEARCH_TEXT = "scott SC SN # number"
SCOTT_REGEX = re.compile(r'''
    (?i)
    \b
    (?:
        (?:(?:Scott|SC|SN)\s*\#?)\s*
    )?
    ([A-Z]?\d{1,4}[A-Z]?)
    \b
''', re.VERBOSE)

EXCLUDE_REGEX = re.compile(r'''
    ^(?:
        \d+[./]\d+|
        \d+(?:st|nd|rd|th)|
        [€£$¢]\d+
    )$
''', re.VERBOSE)

YEAR_REGEX    = re.compile(r'^(18|19|20)\d{2}$', re.IGNORECASE)
DENOM_REGEX   = re.compile(r'^\d+(?:c|d|p|¢)$', re.IGNORECASE)
SCOTT_PREFIX_REGEX  = re.compile(r'(Scott|Sc|SC|SN)\s*#?\s*', re.IGNORECASE)
CATALOG_CONTEXT_REGEX = re.compile(r'\b(used|mint|perf|wmk|watermark|variety)\b', re.IGNORECASE)
OTHER_CATALOG_REGEX   = re.compile(r'\b(SG|Michel|Yvert|Stanley)\b', re.IGNORECASE)


class AsyncGoogleLensController:
    def __init__(self):
        self._playwright      = None
        self._owns_playwright = True
        self.browser          = None
        self.context          = None
        self.page             = None

    async def start(self, playwright=None):
        # Accept a shared Playwright instance to avoid spawning a second Node.js
        # driver process alongside the Colnect controller's. Two concurrent driver
        # processes both pipe into the same Windows IOCP handle, which triggers an
        # access violation in asyncio's _poll under load. One driver manages both
        # browsers without any loss of isolation or concurrency.
        if playwright is not None:
            self._playwright      = playwright
            self._owns_playwright = False
        else:
            self._playwright      = await async_playwright().start()
            self._owns_playwright = True
        self.browser = await self._playwright.chromium.launch(
            headless=LENS_HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context = await self.browser.new_context()
        self.page    = await self.context.new_page()
        await self.page.goto("https://lens.google.com/", timeout=60000)

    async def search(self, image_path: str) -> list:
        try:
            await self.page.goto("https://lens.google.com/", timeout=60000)
            file_input = self.page.locator("input[type='file']:not([hidden])")
            await file_input.set_input_files(image_path)
            await self.page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)
            return await self._scrape_visual_matches()
        except TimeoutError:
            print("Lens search failed: file input not found or timed out")
            return []

    async def _scrape_visual_matches(self) -> list:
        results = []
        containers = await self.page.query_selector_all(
            "div[id=search] div:has(div[jsslot])"
        )
        seen_srcs = set()

        for container in containers:
            try:
                images = await container.query_selector_all("img")
                for img in images:
                    src = await img.get_attribute("src")
                    if not src or src in seen_srcs:
                        continue

                    width  = await img.evaluate("el => el.width")
                    height = await img.evaluate("el => el.height")
                    if width < 50 or height < 50:
                        continue

                    seen_srcs.add(src)

                    site_handle = await img.query_selector("xpath=ancestor::a | following::a")
                    if not site_handle:
                        continue
                    site_link = await site_handle.get_attribute("href")
                    if not site_link:
                        continue

                    if not any(site in site_link.lower() for site in ALLOWED_DOMAINS):
                        continue

                    title_div = await img.query_selector("xpath=ancestor::div[@aria-label][1]")
                    title = (await title_div.get_attribute("aria-label")) if title_div else ""

                    results.append({
                        "thumbnail_url": src,
                        "link":          site_link,
                        "title":         title or (await img.get_attribute("alt")) or "",
                    })

                    if len(results) >= 8:
                        return results

            except Exception:
                continue

        return results

    def filter_results_by_domain(self, results: list) -> list:
        return [r for r in results if any(d in r.get("link", "") for d in ALLOWED_DOMAINS)]

    def extract_scott_numbers(self, results: list) -> str | None:
        candidate_scores: dict[str, list[int]] = defaultdict(list)

        for item in results:
            title = item.get("title", "")
            for match in SCOTT_REGEX.finditer(title):
                candidate = match.group(1)
                score = 0

                prefix_pattern = SCOTT_PREFIX_REGEX.pattern.format(re.escape(candidate))
                if re.search(prefix_pattern, title):
                    score += 5
                if re.match(r'^[A-Z]\d', candidate) or re.search(r'[A-Z]$', candidate, re.IGNORECASE):
                    score += 2
                if re.search(rf'\#{candidate}\b', title):
                    score += 3
                if CATALOG_CONTEXT_REGEX.search(title):
                    score += 1
                if OTHER_CATALOG_REGEX.search(title):
                    score += 1
                if YEAR_REGEX.match(candidate):
                    score -= 3
                if DENOM_REGEX.match(candidate):
                    score -= 4

                candidate_scores[candidate].append(score)

        if not candidate_scores:
            return None

        aggregated = {c: sum(s) for c, s in candidate_scores.items()}
        return max(aggregated.items(), key=lambda x: x[1])[0]

    def extract_country(self, results: list) -> str | None:
        """Return the most frequently mentioned country name across all result titles."""
        counts: Counter = Counter()
        for item in results:
            title_lower = item.get("title", "").lower()
            found: set[str] = set()
            for name_lower in _SORTED_NAMES:
                if len(name_lower) < 4:   # skip 2-3 char codes — too noisy
                    continue
                if re.search(r'\b' + re.escape(name_lower) + r'\b', title_lower):
                    found.add(_COUNTRY_NAMES[name_lower])
            for canonical in found:
                counts[canonical] += 1
        if not counts:
            return None
        return counts.most_common(1)[0][0]

    async def shutdown(self):
        for attr, label in [("context", "lens context"), ("browser", "lens browser")]:
            obj = getattr(self, attr, None)
            if obj:
                try:
                    await obj.close()
                except Exception as e:
                    print(f"Error closing {label}: {e}")
        # Only stop the driver if owned; if it was passed in, the owner tears it down.
        if self._owns_playwright and self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                print(f"Error closing playwright: {e}")
