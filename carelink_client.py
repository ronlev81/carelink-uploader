"""
CareLink EU client using Bearer token from env var CARELINK_TOKEN.
Token is obtained manually from browser cookies (auth_tmp_token).
"""
import os
import time
import requests

CARELINK_EU_BASE = "https://carelink.minimed.eu"


class CareLinkClient:
    def __init__(self, username=None, password=None, country="eu"):
        self.token = os.environ.get("CARELINK_TOKEN", "")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
        })

    def login(self) -> bool:
        if not self.token:
            print("ERROR: CARELINK_TOKEN not set")
            return False
        print("Using CARELINK_TOKEN from environment")
        return True

    def getRecentData(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        r = self.session.get(
            f"{CARELINK_EU_BASE}/patient/data/sharing/display",
            headers=headers,
        )
        print(f"Data fetch: {r.status_code}")
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                print(f"Non-JSON: {r.text[:300]}")
        elif r.status_code == 401:
            print("Token expired — update CARELINK_TOKEN in Railway")
        else:
            print(f"Body: {r.text[:200]}")
        return None
