# colnect_controller.py
from playwright.sync_api import sync_playwright, TimeoutError
import time

class ColnectController:
    username = "digitalAxolotl"
    def __init__(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled"]
        )
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.goto("https://colnect.com/en", timeout=60000)

    def login(self):
        try:
            self.page.wait_for_load_state("networkidle", timeout=5000)
            # Click the login button to open the login modal
            login_trigger = self.page.locator(".login")
            #login_trigger.wait_for(state="visible", timeout=5000)
            login_trigger.click()

            # Wait for the modal inputs to appear
            username_input = self.page.locator("input[id='signin_username']")
            #username_input.wait_for(state="visible", timeout=5000)
            username_input.fill(self.username)

            password_input = self.page.locator("input[id='signin_password']")
            #password_input.wait_for(state="visible", timeout=5000)
            password_input.fill("stampsRneat")

            # Click the submit button inside the modal
            submit_btn = self.page.locator("input[id='signin_btn']")
            #submit_btn.wait_for(state="visible", timeout=5000)
            submit_btn.click()

            # Optionally wait for a known element or URL to confirm login succeeded
            time.sleep(2)
            #self.page.get_by_text("Hello, " + self.username)
            #self.page.wait_for_load_state("networkidle", timeout=10000)

            self.page.goto("https://colnect.com/en/stamps/countries/not_ignored/" + self.username)
            return True
        except Exception:
            print("Login failed or timed out:")
            return False


    def search_stamp(self, scott_number, country):
        try:
            country = country.replace(" ", "_")
            self.page.goto("https://colnect.com/en/stamps/countries/not_ignored/digitalAxolotl", timeout=6000)
            self.page.wait_for_load_state("domcontentloaded", timeout=1000)
            print ("Country: ", country)
            print ("Scott number: ", scott_number)
            country_select = self.page.query_selector("a[href*='" + country + "']")
            print ("Country select:", country_select)
            link = country_select.get_attribute("href")

            print("Selected country:", country_select)
            print ("Link:", link)
            self.page.goto("https://colnect.com/" + link)
            print ("PAGE: ", self.page.url)
            print ("Clicked country:", country)

            # assemble URL with existing URL parts + scott number + change years to list to show stamps instead of years
            prefix, rest = self.page.url.split("/country/", 1)
            country_part, tail = rest.split("/", 1)
            url = f"{prefix}/country/{country_part}/catalog_code/{scott_number}/{tail}".replace("/years/", "/list/", 1)
            print(url)

            self.page.goto(url)
        except Exception as e:
            print("Search failed:", e)
            return False


    # grab stamp info from current page and return it
    def get_colnect_info(self):
        try:
            self.page.wait_for_selector("span#name:not(:empty)")
            #@note TO DO: grab scott number???
            fields = {
                "name": "span[id='name']",
                "scott_number": "dd:has(strong:has-text('Stamp Number'))",
                "series": "dt:has-text('Series') + dd",
                "issued_date": "dd[itemprop='releaseDate'], dd[itemprop='issued']",
                "expired_date": "dt:has-text('Expiry date') + dd",
                "size": "dt:has-text('Size') + dd",
                "colors": "dt:has-text('Colors') + dd",
                "designers": "dt:has-text('Designers') + dd",
                "format": "dt:has-text('Format') + dd",
                "emission": "dt:has-text('Emission') + dd",
                "perforation": "dt:has-text('Perforation') + dd",
                "printing": "dt:has-text('Printing') + dd",
                "paper": "dt:has-text('Paper') + dd",
                "gum": "dt:has-text('Gum') + dd",
                "face_value": "dt:has-text('Face value') + dd",
                "print_run": "dt:has-text('Print run') + dd",
                "watermark": "dt:has-text('Watermark') + dd",
                "description": "dt:has-text('Description') + dd",
                "variants": "dt:has-text('Variants') + dd",
                "themes": "dt:has-text('Themes') + dd",
            }
            # Grab the data for all the fields
            collected_data = {key: self.safe_get_text(selector) for key, selector in fields.items()}

            scott_number = collected_data.get("scott_number").split("Stamp Number", 1)[1].splitlines()[0].strip()
            scott_number.
            collected_data["scott_number"] = scott_number

            # Get the themes and split by "|"
            themes_raw = collected_data.get("themes", "")
            themes = [theme.strip() for theme in themes_raw.split("|")] if themes_raw else []

            # Add themes to the collected data
            collected_data["themes"] = themes

            print(collected_data)
            return collected_data
        except Exception as e:
            print("Error extracting stamp info:", e)
            return {}

    def safe_get_text(self, selector):
        try:
            print ("Getting text for selector:", self.page.locator(selector))
            loc = self.page.locator(selector)
            if loc.count() == 0:
                print ("No locator found for selector:", selector)
                return None
            print ("Inner text for selector", selector, ":", loc.inner_text().strip())
            return loc.inner_text().strip()
        except Exception:
            return None


    def shutdown(self):
        self.context.close()
        self.browser.close()
        self.playwright.stop()
