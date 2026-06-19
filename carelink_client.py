"""
CareLink EU client using Playwright headless browser.
Playwright navigates the Auth0 login page like a real browser,
then transfers session cookies to a requests session for fast data polling.
"""
import time
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

CARELINK_EU_BASE = "https://carelink.minimed.eu"


class CareLinkClient:
    def __init__(self, username, password, country="eu"):
        self.username = username
        self.password = password
        self.country  = country
        self.session  = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        })

    def login(self) -> bool:
        print("Launching headless browser...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context()
            page    = context.new_page()

            try:
                # Navigate to CareLink SSO → Auth0
                url = (f"{CARELINK_EU_BASE}/patient/sso/login"
                       f"?country={self.country}&lang=en")
                page.goto(url, wait_until="networkidle", timeout=30000)
                print(f"Reached: {page.url[:80]}")

                # Step 1: fill username and submit
                page.wait_for_selector('input[name="username"]', timeout=15000)
                page.fill('input[name="username"]', self.username)
                print("Username entered")
                page.click('button[type="submit"]')
                page.wait_for_load_state("networkidle", timeout=15000)

                # Step 2: fill password page — also re-fill username if visible
                page.wait_for_selector('input[name="password"]', timeout=15000)
                # Re-fill username in case Auth0 shows it again on password page
                for sel in ['input[name="username"]', 'input[name="email"]']:
                    try:
                        el = page.query_selector(sel)
                        if el:
                            page.fill(sel, self.username)
                            print(f"Re-filled {sel} on password page")
                    except Exception:
                        pass
                page.fill('input[name="password"]', self.password)
                print("Password entered")

                # Submit login
                page.click('button[type="submit"]')
                print("Submit clicked, waiting for navigation...")

                # Wait for any navigation to complete (up to 60s)
                try:
                    page.wait_for_url(f"{CARELINK_EU_BASE}/**", timeout=60000)
                    print(f"Landed on CareLink: {page.url[:80]}")
                except PlaywrightTimeout:
                    print(f"wait_for_url timeout, current URL: {page.url[:80]}")
                    # Maybe we're on an intermediate page — wait for idle and check
                    page.wait_for_load_state("networkidle", timeout=15000)
                    print(f"After idle wait: {page.url[:80]}")
                    if CARELINK_EU_BASE not in page.url:
                        # Print page text to see what's on screen
                        try:
                            body = page.inner_text("body")
                            print(f"Page content: {body[:400]}")
                        except Exception:
                            pass

            except PlaywrightTimeout as e:
                print(f"Timeout: {e}")
                print(f"Current URL: {page.url[:80]}")
                # Check for error message on page
                try:
                    err = page.query_selector('[class*="error"], [class*="alert"]')
                    if err:
                        print(f"Page error: {err.inner_text()}")
                except Exception:
                    pass
                browser.close()
                return False

            # Transfer cookies to requests session
            for cookie in context.cookies():
                self.session.cookies.set(
                    cookie["name"], cookie["value"],
                    domain=cookie.get("domain", "").lstrip(".")
                )
            cookie_names = [c["name"] for c in context.cookies()]
            print(f"Cookies transferred: {cookie_names}")
            browser.close()

        return True

    def getRecentData(self):
        params = {
            "cpSerialNumber": "NONE",
            "msgType":        "last24hours",
            "requestTime":    int(time.time() * 1000),
        }
        r = self.session.get(
            f"{CARELINK_EU_BASE}/patient/connect/data",
            params=params,
            headers={"Accept": "application/json"},
        )
        print(f"Data fetch: {r.status_code}")
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                print(f"Non-JSON: {r.text[:300]}")
        else:
            print(f"Body: {r.text[:200]}")
        return None
