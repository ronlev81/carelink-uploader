"""
CareLink EU client.
Auth strategy:
  1. GET /patient/sso/login  → follows to Auth0 /u/login?state=XXX
  2. POST credentials directly to that same URL (Auth0 ULP accepts it)
  3. Auth0 redirects → CareLink callback → session cookie set
  4. GET /patient/connect/data with session cookie
"""
import re
import time
import requests
from urllib.parse import urlparse, parse_qs

CARELINK_EU_BASE = "https://carelink.minimed.eu"


class CareLinkClient:
    def __init__(self, username, password, country="eu"):
        self.username = username
        self.password = password
        self.country  = country
        self.session  = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            ),
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def login(self) -> bool:
        # Step 1: follow SSO redirect to Auth0 login page
        r1 = self.session.get(
            f"{CARELINK_EU_BASE}/patient/sso/login",
            params={"country": self.country, "lang": "en"},
            allow_redirects=True,
        )
        login_url = r1.url
        print(f"Auth0 login page: {r1.status_code} → {login_url[:90]}")

        if r1.status_code != 200 or "login" not in login_url:
            print("ERROR: did not reach Auth0 login page")
            return False

        # Step 2: POST credentials to the SAME URL (Auth0 ULP accepts POST here)
        payload = {
            "username": self.username,
            "password": self.password,
            "action":   "default",
        }
        r2 = self.session.post(
            login_url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer":      login_url,
                "Origin":       f"https://{urlparse(login_url).netloc}",
            },
            allow_redirects=True,
        )
        print(f"POST result: {r2.status_code} → {r2.url[:90]}")

        if "Wrong email or password" in r2.text or "wrong-email-password" in r2.text:
            print("ERROR: wrong username or password")
            return False

        cookies = list(self.session.cookies.keys())
        print(f"Cookies: {cookies}")

        # If still on Auth0, try to follow any redirect back to CareLink
        if CARELINK_EU_BASE not in r2.url:
            print("Not yet on CareLink, probing dashboard...")
            r3 = self.session.get(
                f"{CARELINK_EU_BASE}/patient/connect/data",
                params={"cpSerialNumber": "NONE", "msgType": "last24hours",
                        "requestTime": int(time.time() * 1000)},
                headers={"Accept": "application/json"},
            )
            print(f"Probe: {r3.status_code}")
            if r3.status_code == 200:
                print("Login OK (data accessible)")
                return True
            print(f"Probe body: {r3.text[:200]}")
            return False

        print("Login OK")
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
