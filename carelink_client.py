"""
CareLink EU client.
Endpoint: POST /patient/v2/monitor/data  {"patientUsername": "<username>"}
Auth:     Authorization: Bearer <CARELINK_TOKEN>
"""
import os
import time
import requests

CARELINK_EU_BASE = "https://carelink.minimed.eu"


class CareLinkClient:
    def __init__(self, username=None, password=None, country="eu"):
        self.token    = os.environ.get("CARELINK_TOKEN", "")
        self.username = os.environ.get("CARELINK_USERNAME", username or "")
        self.session  = requests.Session()
        self.session.headers.update({
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept":          "application/json, text/plain, */*",
            "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8",
            "Content-Type":    "application/json",
        })

    def login(self) -> bool:
        if not self.token:
            print("ERROR: CARELINK_TOKEN not set")
            return False
        if not self.username:
            print("ERROR: CARELINK_USERNAME not set")
            return False
        print(f"Using Bearer token for user: {self.username}")
        return True

    def getRecentData(self):
        r = self.session.post(
            f"{CARELINK_EU_BASE}/patient/v2/monitor/data",
            json={"patientUsername": self.username},
            headers={"Authorization": f"Bearer {self.token}"},
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
            print(f"Body: {r.text[:300]}")
        return None
