"""
CareLink EU client.
Glucose endpoint: GET https://clcloud.minimed.eu/connect/retina/v1/personalWebView
Auth: Authorization: Bearer <CARELINK_TOKEN>
Response: list of {"sg": <mg/dL>, "ts": <unix seconds>}
"""
import os
import time
import requests

CLCLOUD_BASE = "https://clcloud.minimed.eu"


class CareLinkClient:
    def __init__(self, username=None, password=None, country="eu"):
        self.token   = os.environ.get("CARELINK_TOKEN", "")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept":          "application/json, text/plain, */*",
            "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8",
        })

    def login(self) -> bool:
        if not self.token:
            print("ERROR: CARELINK_TOKEN not set")
            return False
        print("Using Bearer token for clcloud API")
        return True

    def getRecentData(self):
        r = self.session.get(
            f"{CLCLOUD_BASE}/connect/retina/v1/personalWebView",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        print(f"Data fetch: {r.status_code}")
        if r.status_code == 200:
            try:
                data = r.json()
                # Find the most recent reading by highest ts
                readings = []
                if isinstance(data, list):
                    readings = data
                elif isinstance(data, dict):
                    # Might be nested — search for list with sg/ts
                    for v in data.values():
                        if isinstance(v, list) and v and "sg" in v[0]:
                            readings = v
                            break

                if readings:
                    latest = max(readings, key=lambda x: x.get("ts", 0))
                    print(f"Latest: sg={latest.get('sg')} ts={latest.get('ts')}")
                    return latest
                else:
                    print(f"Unexpected structure: {str(data)[:300]}")
            except Exception as e:
                print(f"Parse error: {e} — {r.text[:300]}")
        elif r.status_code == 401:
            print("Token expired — update CARELINK_TOKEN in Railway")
        else:
            print(f"Body: {r.text[:300]}")
        return None
