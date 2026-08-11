"""
Colnect.com integration controller (async Playwright).
"""
import asyncio
from playwright.async_api import async_playwright, TimeoutError
from config import COLNECT_USERNAME, COLNECT_PASSWORD, COLNECT_HEADLESS
from logger import logger


class AsyncColnectController:
    def __init__(self):
        if not COLNECT_USERNAME or not COLNECT_PASSWORD:
            raise ValueError(
                "Colnect credentials not configured. "
                "Set COLNECT_USERNAME and COLNECT_PASSWORD environment variables."
            )
        self.username         = COLNECT_USERNAME
        self.password         = COLNECT_PASSWORD
        self._playwright      = None
        self._owns_playwright = True
        self.browser          = None
        self.context          = None
        self.page             = None

    async def start(self, playwright=None):
        # Accept a shared Playwright instance to avoid spawning a second Node.js
        # driver process alongside the Lens controller's. Two concurrent driver
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
            headless=COLNECT_HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context = await self.browser.new_context()
        self.page    = await self.context.new_page()
        await self.page.goto("https://colnect.com/en", timeout=60000)
        logger.info("Colnect controller initialized")

    async def login(self) -> bool:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=5000)
            await self.page.locator(".login").click()

            await self.page.locator("input[id='signin_username']").fill(self.username)
            await self.page.locator("input[id='signin_password']").fill(self.password)
            await self.page.locator("input[id='signin_btn']").click()

            await asyncio.sleep(2)
            await self.page.goto(
                f"https://colnect.com/en/stamps/countries/not_ignored/{self.username}",
                timeout=10000,
            )
            logger.info(f"Logged into Colnect as {self.username}")
            return True
        except Exception as e:
            logger.error(f"Colnect login failed: {e}")
            return False

    async def _navigate_to_country(self, country: str) -> bool:
        """Navigate the page to the best-matching Colnect country page for
        `country`. On success returns True with self.page on a /country/ URL;
        returns False if no match is found. Exceptions propagate to the caller's
        error handler. Shared by search_stamp and search_stamp_by_filter."""
        collection_url = f"https://colnect.com/en/stamps/countries/not_ignored/{self.username}"

        await self.page.goto(collection_url, timeout=8000)

        # state='attached' so hidden sub-country links inside collapsed
        # div.sub_countries containers are included in the DOM query.
        try:
            await self.page.wait_for_selector(
                "a.country_flag[href]", state="attached", timeout=8000
            )
        except Exception:
            logger.warning("Timed out waiting for country links; proceeding anyway")

        # Extract all country flag titles and hrefs in one JS round-trip instead
        # of one IPC call per link (which adds up to hundreds of calls).
        # Fall back to the href slug as a display name when the title attribute
        # is absent — some links omit it, which would otherwise score as "" and
        # lose to unrelated substring matches like "Austria-Hungary" > "Hungary".
        data = await self.page.evaluate("""
            () => [...document.querySelectorAll('a.country_flag[href]')]
                  .map(a => {
                    const href = a.getAttribute('href') || '';
                    const m    = href.match(/\\/country\\/\\d+-([^/?]+)/);
                    const slug = m ? m[1].replace(/_/g, ' ') : '';
                    return [a.getAttribute('title') || slug, href];
                  })
        """)
        if not data:
            logger.error("No country links found on Colnect collection page")
            return False

        titles = [d[0] for d in data]
        hrefs  = [d[1] for d in data]

        # Score by title attribute — that's the canonical display name and is
        # reliable for both top-level countries and sub-countries.  The href slug
        # often has the parent territory prepended (e.g. "Malaya_Federated_Malay_States")
        # which would break a name-in-href substring match for stored sub-country names.
        target = country.lower()

        def country_score(title: str) -> int:
            t = title.lower()
            if t == target:
                return 0
            if t.startswith(target):
                rest = t[len(target):]
                # Space/letter suffix → longer official name ("United Kingdom of Great Britain...")
                if rest and (rest[0] == ' ' or rest[0].isalpha()):
                    return 1
                # Punctuation suffix → annotation/category ("United Kingdom: Illegal Stamps")
                return 2
            if target in t:  return 3  # target embedded elsewhere ("Austria-Hungary" for "Hungary")
            if t in target:  return 4
            return 5

        # Within each score tier, prefer shorter titles (more specific to the search term).
        best_idx = min(range(len(titles)), key=lambda i: (country_score(titles[i]), len(titles[i])))
        score    = country_score(titles[best_idx])
        href     = hrefs[best_idx]
        logger.debug(f"Selected country '{titles[best_idx]}' (score {score}): {href}")

        if not href or score == 5:
            logger.error(f"Could not find matching country link for: {country}")
            return False

        # href is already an absolute path starting with '/'; avoid double-slash.
        await self.page.goto(f"https://colnect.com/{href.lstrip('/')}", timeout=6000)
        logger.info(f"Navigated to country page: {self.page.url}")

        if "/country/" not in self.page.url:
            logger.error(
                f"Country navigation redirected away from expected URL — "
                f"landed on '{self.page.url}' (expected a /country/ URL). "
                f"This usually means you are not logged in to Colnect or the "
                f"selected href '{href}' is invalid."
            )
            return False

        return True

    async def search_stamp(self, scott_number: str, country: str) -> bool:
        try:
            logger.info(f"Searching for stamp: Scott #{scott_number}, Country: {country}")

            if not await self._navigate_to_country(country):
                return False

            try:
                prefix, rest          = self.page.url.split("/country/", 1)
                country_part, tail    = rest.split("/", 1)
                catalog_url = (
                    f"{prefix}/country/{country_part}/catalog_code/{scott_number}/{tail}"
                    .replace("/years/", "/list/", 1)
                )
                logger.info(f"Navigating to stamp catalog: {catalog_url}")
                await self.page.goto(catalog_url, timeout=6000)
                await asyncio.sleep(1)
                logger.info(f"Successfully navigated to stamp page: {self.page.url}")
                return True
            except ValueError as e:
                logger.error(f"Failed to parse Colnect URL structure: {e}")
                return False

        except TimeoutError as e:
            logger.error(f"Colnect navigation timeout: {e}")
            return False
        except Exception as e:
            logger.error(f"Colnect stamp search failed: {e}")
            return False

    # Filters for the alternate (non-Scott) Colnect search, in the order they are
    # applied to the URL. Each stamp list URL is
    #   .../stamps/list/country/<id-Name>/<seg1>/<val1>/<seg2>/<val2>/.../<tail>
    # and every filter contributes one "<segment>/<value>" pair. Two kinds:
    #   * "direct" — the value goes straight into the URL (e.g. year → year/1998).
    #   * "lookup" — the value is an opaque code (e.g. face_value/637-090) that
    #     can't be built from the label, so instead open Colnect's "<plural>" chooser
    #     page (built from the country + all filters resolved *before* it) and
    #     match the visible option label to recover the code.
    # Order matters and mirrors Colnect's own canonical URL order: theme sits
    # right after the country, then year, and face_value is always last (it is
    # the deepest lookup, scoped by every filter before it). A filter that should
    # constrain a later lookup's chooser page must appear before it. To add a new
    # dimension, insert a spec here and pass its value in `filters`; lookup
    # dimensions reuse _resolve_filter_option unchanged.
    _FILTER_SPECS = [
        {"key": "theme",      "segment": "theme",      "kind": "lookup", "plural": "themes"},
        {"key": "year",       "segment": "year",       "kind": "direct"},
        {"key": "face_value", "segment": "face_value", "kind": "lookup", "plural": "face_values"},
    ]

    async def search_stamp_by_filter(self, country: str, filters: dict) -> bool:
        """Alternate search: select the country, then apply whichever of the
        `filters` (keyed by _FILTER_SPECS 'key') are non-empty. Blank/missing
        filters are skipped, so a partial search (e.g. year only, or face value
        only) is fine. Returns True once navigated to the filtered stamp list."""
        try:
            filters = filters or {}
            active  = {s["key"]: v for s in self._FILTER_SPECS
                       if (v := (filters.get(s["key"]) or "").strip())}
            logger.info(f"Filter search — Country: {country}, Filters: {active}")

            if not await self._navigate_to_country(country):
                return False

            try:
                prefix, rest       = self.page.url.split("/country/", 1)
                country_part, tail = rest.split("/", 1)
            except ValueError as e:
                logger.error(f"Failed to parse Colnect country URL structure: {e}")
                return False
            # prefix is like ".../stamps/years"; drop the trailing segment to get
            # the ".../stamps" base every list/chooser URL is built from.
            base = prefix.rsplit("/", 1)[0]

            def _url(kind: str, segs: list[str]) -> str:
                mid = "/".join(segs)
                mid = f"{mid}/" if mid else ""
                return f"{base}/{kind}/country/{country_part}/{mid}{tail}"

            # Accumulate resolved "<segment>/<value>" pairs in spec order.
            segments: list[str] = []
            for spec in self._FILTER_SPECS:
                value = active.get(spec["key"])
                if not value:
                    continue
                seg = spec["segment"]
                if spec["kind"] == "direct":
                    segments.append(f"{seg}/{value}")
                    continue
                # lookup: resolve the opaque code from the chooser page, which is
                # scoped by every filter resolved so far (segments).
                href = await self._resolve_filter_option(
                    _url(spec["plural"], segments), seg, value)
                if not href:
                    return False
                code = self._segment_value(href, seg)
                if not code:
                    logger.error(f"Could not read {seg} code from resolved href: {href}")
                    return False
                segments.append(f"{seg}/{code}")

            list_url = _url("list", segments)
            logger.info(f"Navigating to filtered stamp list: {list_url}")
            await self.page.goto(list_url, timeout=6000)
            await asyncio.sleep(1)
            logger.info(f"Successfully navigated to filtered stamp list: {self.page.url}")
            return True

        except TimeoutError as e:
            logger.error(f"Colnect filter navigation timeout: {e}")
            return False
        except Exception as e:
            logger.error(f"Colnect filter search failed: {e}")
            return False

    @staticmethod
    def _segment_value(url: str, segment: str) -> str | None:
        """Return the value that follows /<segment>/ in a Colnect URL, e.g.
        _segment_value('.../face_value/637-090/...', 'face_value') -> '637-090'."""
        marker = f"/{segment}/"
        i = url.find(marker)
        return url[i + len(marker):].split("/", 1)[0] if i != -1 else None

    async def _resolve_filter_option(self, chooser_url: str, segment: str,
                                     target: str) -> str | None:
        """Open a Colnect filter chooser page and return the stamp-list href for
        the option whose visible label exactly matches `target`, or None. Used
        for every "lookup" filter (face value today, others later)."""
        logger.info(f"Loading {segment} filters: {chooser_url}")
        await self.page.goto(chooser_url, timeout=6000)

        # The option anchors are finalised by a JS pass that runs after the 'load'
        # event (the container is tagged data-counters-processed once done), so
        # goto can return before they exist. Wait for at least one to attach.
        try:
            await self.page.wait_for_selector(
                f"a[href*='/{segment}/']", state="attached", timeout=8000
            )
        except Exception:
            logger.warning(
                f"No {segment} option links appeared at {self.page.url}; "
                f"reading whatever is present"
            )

        # Match the visible label exactly. Colnect omits currency symbols but
        # keeps characters like '*', '+', and '()', so an exact string compare
        # (no regex) is what's wanted. Restrict to anchors whose href carries the
        # /<segment>/ pair so sidebar filters can't be mismatched. Return the
        # option count + labels too, so a miss is distinguishable in the log from
        # an empty or mislanded page.
        result = await self.page.evaluate(
            """
            ([segment, target]) => {
                const clean = (s) => (s || '').replace(/\\u00a0/g, ' ').trim();
                // span.content is filled by the same late JS pass and is empty
                // under automation, so fall back to the server-rendered title
                // attribute ("Stamps: <value>") for the label.
                const labelOf = (a) => {
                    const span    = a.querySelector('span.content');
                    const spanTxt = span ? clean(span.textContent) : '';
                    if (spanTxt) return spanTxt;
                    return clean(a.getAttribute('title') || '').replace(/^stamps:\\s*/i, '');
                };
                const opts   = [...document.querySelectorAll('a[href*="/' + segment + '/"]')];
                const labels = [];
                for (const a of opts) {
                    const label = labelOf(a);
                    labels.push(label);
                    if (label === target)
                        return {href: a.getAttribute('href'), count: opts.length, labels};
                }
                return {href: null, count: opts.length, labels};
            }
            """,
            [segment, target],
        )
        href = result.get("href")
        if not href:
            logger.error(
                f"No {segment} option matched '{target}' — "
                f"{result.get('count', 0)} options present at {self.page.url}: "
                f"{result.get('labels', [])[:40]}"
            )
            return None
        return href

    # Maps each output field to the Colnect definition-list <dt> label whose
    # following <dd> holds the value. Extraction happens entirely in the page's
    # JS context (see get_colnect_info) so label matching, multi-match handling,
    # and CSS-hidden text all resolve in one round-trip.
    _LABEL_FIELDS = {
        "series":       "Series",
        "expired_date": "Expiry date",
        "size":         "Size",
        "colors":       "Colors",
        "designers":    "Designers",
        "format":       "Format",
        "emission":     "Emission",
        "perforation":  "Perforation",
        "printing":     "Printing",
        "paper":        "Paper",
        "gum":          "Gum",
        "face_value":   "Face value",
        "print_run":    "Print run",
        "watermark":    "Watermark",
        "description":  "Description",
        "variants":     "Variants",
    }

    async def get_colnect_info(self) -> dict:
        try:
            await self.page.wait_for_selector("span#name:not(:empty)", timeout=5000)

            # Extract every field in a single page.evaluate() rather than one
            # Playwright locator round-trip per field. Besides being ~50x fewer
            # IPC hops (the old per-field approach was the main source of the
            # slowdown), reading textContent in JS sidesteps two failure modes of
            # the locator approach:
            #   * strict-mode violations when a `dt:has-text(...) + dd` substring
            #     selector resolved to more than one element (silently dropped the
            #     field), and
            #   * inner_text() returning "" for CSS-hidden <i>/<a> wrappers, which
            #     previously required a scatter of text_content() fallbacks.
            collected = await self.page.evaluate(
                """
                (labelFields) => {
                    const clean = (s) => (s || '').replace(/\\u00a0/g, ' ').trim();
                    const norm  = (s) => clean(s).replace(/\\s+/g, ' ').toLowerCase();

                    // Build a label -> <dd> map from every <dt>/<dd> pair once.
                    const byLabel = {};
                    document.querySelectorAll('dt').forEach((dt) => {
                        let dd = dt.nextElementSibling;
                        while (dd && dd.tagName !== 'DD') dd = dd.nextElementSibling;
                        if (!dd) return;
                        const key = norm(dt.textContent);
                        if (!(key in byLabel)) byLabel[key] = dd;  // first wins
                    });

                    // Match a label exactly, then by prefix, then substring —
                    // mirrors the old dt:has-text() substring behaviour.
                    const ddFor = (label) => {
                        const l = norm(label);
                        if (byLabel[l]) return byLabel[l];
                        for (const k in byLabel) if (k.startsWith(l)) return byLabel[k];
                        for (const k in byLabel) if (k.includes(l))   return byLabel[k];
                        return null;
                    };
                    const valFor = (label) => {
                        const dd = ddFor(label);
                        return dd ? (clean(dd.textContent) || null) : null;
                    };

                    const out = {};
                    out.name = clean((document.querySelector('span#name') || {}).textContent);
                    for (const key in labelFields) out[key] = valFor(labelFields[key]);

                    // Issued date: prefer the semantic itemprop, fall back to label.
                    const issued = document.querySelector(
                        "dd[itemprop='releaseDate'], dd[itemprop='issued']");
                    out.issued_date = issued ? (clean(issued.textContent) || null)
                                             : valFor('Issued');

                    // Scott number lives in a catalog-codes <dd> that lists several
                    // catalogues, each as "<strong>Name</strong> value<br>". Find the
                    // <strong> labelled "Stamp Number" (there are several strongs in the
                    // dd, so can't just take the first) and read the text nodes after
                    // it up to the next <br>/<strong> — <br> separators aren't newlines
                    // in textContent, so can't rely on line splitting.
                    out.scott_number = null;
                    scott:
                    for (const dd of document.querySelectorAll('dd')) {
                        for (const strong of dd.querySelectorAll('strong')) {
                            if (!norm(strong.textContent).includes('stamp number')) continue;
                            let val = '', node = strong.nextSibling;
                            while (node && !(node.nodeType === 1 &&
                                   (node.tagName === 'BR' || node.tagName === 'STRONG'))) {
                                val += node.textContent || '';
                                node = node.nextSibling;
                            }
                            val = clean(val).replace(/^[:\\s]+/, '').trim();
                            if (val) { out.scott_number = val; break scott; }
                        }
                    }

                    // Country: page label first, then the trailing URL segment
                    // (Colnect stamp URLs end with /Country_Name, spaces underscored).
                    let country = valFor('Country');
                    if (!country) {
                        const parts = location.pathname.replace(/\\/+$/, '').split('/');
                        country = (parts[parts.length - 1] || '').replace(/_/g, ' ').trim();
                    }
                    out.country = country || '';

                    // Themes: prefer the anchor labels, else split the raw text.
                    const themesDd = ddFor('Themes');
                    let themes = [];
                    if (themesDd) {
                        themes = [...themesDd.querySelectorAll('a')]
                            .map((a) => clean(a.textContent)).filter(Boolean);
                        if (!themes.length) {
                            const raw = clean(themesDd.textContent);
                            if (raw) themes = raw.split(/[|,;\\n]+/)
                                .map((t) => t.trim()).filter(Boolean);
                        }
                    }
                    out.themes = themes;

                    // Series URL + name from the <a> inside the series <dd>.
                    const seriesDd   = ddFor('Series');
                    const seriesLink = seriesDd ? seriesDd.querySelector('a') : null;
                    out.series_url = seriesLink ? (seriesLink.getAttribute('href') || null) : null;
                    if (!out.series && seriesLink) {
                        const lt = clean(seriesLink.textContent);
                        if (lt) out.series = lt;
                    }

                    return out;
                }
                """,
                self._LABEL_FIELDS,
            )

            logger.info(f"Extracted stamp info: {collected.get('name') or 'Unknown'}")
            return collected

        except TimeoutError:
            logger.error("Timeout waiting for stamp data on Colnect page")
            return {}
        except Exception as e:
            logger.error(f"Error extracting stamp info from Colnect: {e}")
            return {}

    async def navigate_to(self, url: str):
        await self.page.goto(url, timeout=15000)

    async def shutdown(self):
        for attr, label in [("context", "Colnect context"), ("browser", "Colnect browser")]:
            obj = getattr(self, attr, None)
            if obj:
                try:
                    await obj.close()
                except Exception as e:
                    logger.debug(f"Error closing {label}: {e}")
        # Only stop the driver if owned; if it was passed in, the owner tears it down.
        if self._owns_playwright and self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.debug(f"Error closing Playwright: {e}")
        logger.info("Colnect controller shut down")
