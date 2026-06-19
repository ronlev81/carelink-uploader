"""
CareLink EU client with auto-login.

Auth flow:
  1. GET carelink.minimed.eu/patient/login → redirects to Auth0 login page
  2. POST carelink-login.minimed.eu/u/login?state=<STATE> with credentials
  3. Redirects back → auth_tmp_token cookie set on carelink.minimed.eu
  4. Use token as Bearer for clcloud API calls

Env vars required:
  CARELINK_USERNAME  — login username (e.g. idomedi)
  CARELINK_PASSWORD  — login password
"""
import os
import re
import time
import requests

CARELINK_BASE  = "https://carelink.minimed.eu"
AUTH0_BASE     = "https://carelink-login.minimed.eu"
CLCLOUD_BASE   = "https://clcloud.minimed.eu"

# Token expires in ~40 min — refresh 5 min before expiry
TOKEN_TTL_SEC  = 35 * 60


def _extract_agg_stats(agg):
    if not agg:
        return {}
    tir = agg.get("tir", {})
    sg  = agg.get("sg", {})
    return {
        "tirNormal":   tir.get("normal"),
        "tirHigh":     tir.get("high"),
        "tirLow":      tir.get("low"),
        "tirExtHigh":  tir.get("extHigh"),
        "tirExtLow":   tir.get("extLow"),
        "avgSG":       sg.get("avg"),
        "sdSG":        sg.get("sd"),
        "tdd":         agg.get("tdd"),
        "autoMode":    agg.get("autoMode"),
        "sensorUsage": agg.get("sensorUsage"),
    }


class CareLinkClient:
    def __init__(self, username=None, password=None, country="eu"):
        self.username      = username or os.environ.get("CARELINK_USERNAME", "")
        self.password      = password or os.environ.get("CARELINK_PASSWORD", "")
        self.token         = os.environ.get("CARELINK_TOKEN", "")
        self.token_fetched = 0  # unix timestamp when token was obtained
        self.session       = requests.Session()
        self.session.headers.update({
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8",
        })

    def _do_login(self) -> bool:
        """Perform full Auth0 login flow and store auth_tmp_token."""
        if not self.username or not self.password:
            print("ERROR: CARELINK_USERNAME / CARELINK_PASSWORD not set")
            return False

        try:
            # Step 1: GET login page — follow redirects to Auth0 u/login
            r = self.session.get(
                f"{CARELINK_BASE}/patient/login",
                allow_redirects=True,
                timeout=20,
            )
            # Extract state from URL  (ends up at /u/login?state=...)
            state_match = re.search(r"/u/login\?state=([^&\"']+)", r.url)
            if not state_match:
                # Try to find it in the HTML
                state_match = re.search(r"state=([A-Za-z0-9_\-]+)", r.text)
            if not state_match:
                print(f"Login: could not find Auth0 state. URL={r.url}")
                return False
            state = state_match.group(1)

            # Step 2: POST credentials to Auth0
            login_url = f"{AUTH0_BASE}/u/login?state={state}"
            r2 = self.session.post(
                login_url,
                data={
                    "state":    state,
                    "username": self.username,
                    "password": self.password,
                    "action":   "default",
                },
                allow_redirects=True,
                timeout=20,
            )

            # auth_tmp_token should now be in the session cookie jar
            token = self.session.cookies.get("auth_tmp_token")
            if not token:
                print(f"Login failed — no auth_tmp_token. Final URL: {r2.url}")
                return False

            self.token         = token
            self.token_fetched = time.time()
            print(f"Login OK — new token obtained (expires in ~{TOKEN_TTL_SEC//60} min)")
            return True

        except Exception as e:
            print(f"Login error: {e}")
            return False

    def _token_needs_refresh(self) -> bool:
        if not self.token:
            return True
        age = time.time() - self.token_fetched
        return age >= TOKEN_TTL_SEC

    def login(self) -> bool:
        # If we have a manually-set CARELINK_TOKEN use it for the first run,
        # but still set up credentials for auto-refresh.
        if self.token and not self.token_fetched:
            print("Using pre-set CARELINK_TOKEN (will auto-refresh when it expires)")
            self.token_fetched = time.time()
            return True
        return self._do_login()

    def getRecentData(self):
        """
        Returns dict with glucose, trend, ts, pumpInfo, stats7d/14d/30d.
        Auto-refreshes the token if it's close to expiry.
        Returns None on failure.
        """
        if self._token_needs_refresh():
            print("Token expiring — refreshing...")
            if not self._do_login():
                print("Re-login failed")
                return None

        r = self.session.get(
            f"{CLCLOUD_BASE}/connect/retina/v1/personalWebView",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=20,
        )
        print(f"Data fetch: {r.status_code}")

        if r.status_code == 401:
            print("Token rejected — forcing re-login...")
            self.token_fetched = 0
            if not self._do_login():
                return None
            # Retry once with the new token
            r = self.session.get(
                f"{CLCLOUD_BASE}/connect/retina/v1/personalWebView",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=20,
            )
            if r.status_code != 200:
                print(f"Retry failed: {r.status_code}")
                return None

        if r.status_code != 200:
            print(f"Unexpected status: {r.status_code} — {r.text[:200]}")
            return None

        try:
            data = r.json()
            mgdl = data["ResponsePayload"]["mgdl"]

            # Latest glucose reading
            latest_sg = None
            for day in mgdl.get("Agg1d", []):
                for reading in day.get("sg", {}).get("sgVal", []):
                    if reading.get("sg", 0) > 0:
                        if latest_sg is None or reading.get("ts", 0) > latest_sg.get("ts", 0):
                            latest_sg = reading

            if latest_sg:
                print(f"Latest: sg={latest_sg.get('sg')} ts={latest_sg.get('ts')}")
            else:
                print("No valid glucose reading found")

            agg1d         = mgdl.get("Agg1d", [])
            today_agg     = next((d for d in agg1d if d.get("lastInProgressDay")), agg1d[0] if agg1d else None)
            yesterday_agg = next((d for d in agg1d if not d.get("lastInProgressDay")), None)
            agg7d_list    = mgdl.get("Agg7d",  [])
            agg14d_list   = mgdl.get("Agg14d", [])
            agg30d_list   = mgdl.get("Agg30d", [])

            rp = data.get("ResponsePayload", {})
            dd = rp.get("deviceDetails", {})
            pump_info = {k: v for k, v in {
                "pumpModel":   dd.get("deviceModel"),
                "sensorModel": dd.get("sensorModel"),
            }.items() if v is not None}
            print(f"Pump: {pump_info.get('pumpModel')} sensor: {pump_info.get('sensorModel')}")

            result = {
                "glucose":    latest_sg.get("sg")            if latest_sg else None,
                "rawAgg1d":   agg1d,
                "ts":         latest_sg.get("ts")            if latest_sg else None,
                "trend":      latest_sg.get("trend", "NONE") if latest_sg else "NONE",
                "pumpInfo":   pump_info,
                "statsToday": _extract_agg_stats(today_agg),
                "statsYday":  _extract_agg_stats(yesterday_agg),
                "stats7d":    _extract_agg_stats(agg7d_list[0]  if agg7d_list  else None),
                "stats14d":   _extract_agg_stats(agg14d_list[0] if agg14d_list else None),
                "stats30d":   _extract_agg_stats(agg30d_list[0] if agg30d_list else None),
            }

            s7 = result["stats7d"]
            print(f"7d: TIR={s7.get('tirNormal')}% avg={s7.get('avgSG')} mg/dL TDD={s7.get('tdd')}u")
            return result

        except (KeyError, TypeError) as e:
            print(f"Parse error: {e} — {r.text[:300]}")
            return None
