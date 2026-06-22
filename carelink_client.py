"""
CareLink EU client — web flow with silent token refresh via /patient/sso/reauth.

How it works:
  * Auth is held entirely in a cookie jar (Auth0 session + auth_tmp_token),
    seeded once from a real browser login (see web_session.py / local seeding).
  * The short-lived auth_tmp_token (~50 min) is refreshed silently by
    POSTing to https://carelink.minimed.eu/patient/sso/reauth with the jar.
    No reCAPTCHA, no browser — plain requests, runs anywhere (e.g. Railway).
  * After each reauth the (possibly rolling) jar is handed back via the
    on_cookies_updated callback so the caller can persist it (e.g. Firestore).

No Chromium/Playwright needed at runtime — those are only for the one-time
local cookie seeding.
"""
import json
import time
import base64
import requests

CARELINK_BASE = "https://carelink.minimed.eu"
CLCLOUD_BASE  = "https://clcloud.minimed.eu"
REAUTH_URL    = f"{CARELINK_BASE}/patient/sso/reauth"
DATA_URL      = f"{CLCLOUD_BASE}/connect/retina/v1/personalWebView"
USERS_ME_URL  = f"{CARELINK_BASE}/patient/users/me"

# Refresh the token when it has less than this many seconds of life left.
REFRESH_MARGIN_SEC = 8 * 60

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


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
    def __init__(self, cookies=None, on_cookies_updated=None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
        })
        self.on_cookies_updated = on_cookies_updated
        self.token = None
        self._patient_name = None  # cached from /users/me
        if cookies:
            self.load_cookies(cookies)

    # ---------- cookie jar ----------

    def load_cookies(self, cookies):
        for c in cookies:
            try:
                self.session.cookies.set(
                    c["name"], c["value"],
                    domain=c.get("domain", "").lstrip("."),
                    path=c.get("path", "/"),
                )
            except Exception:
                pass
        self.token = self.session.cookies.get("auth_tmp_token")

    def export_cookies(self):
        out = []
        for c in self.session.cookies:
            out.append({
                "name": c.name, "value": c.value,
                "domain": c.domain, "path": c.path,
            })
        return out

    # ---------- token lifecycle ----------

    def _token_exp_ts(self):
        if not self.token:
            return None
        try:
            payload_b64 = self.token.split(".")[1]
            payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
            return payload.get("exp")
        except Exception:
            return None

    def _needs_reauth(self):
        if not self.token:
            return True
        exp = self._token_exp_ts()
        if exp is None:
            return True
        return (exp - time.time()) < REFRESH_MARGIN_SEC

    def reauth(self):
        """Silent token refresh. Returns True on success."""
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            r = self.session.post(REAUTH_URL, headers=headers, timeout=30)
        except Exception as e:
            print(f"reauth error: {e}")
            return False

        if r.status_code != 200:
            # 401/403 here means the underlying session expired — need a new
            # browser login to re-seed cookies.
            print(f"reauth failed: HTTP {r.status_code} — session likely expired, re-seed cookies")
            return False

        new_token = self.session.cookies.get("auth_tmp_token")
        if not new_token:
            print("reauth returned 200 but no auth_tmp_token cookie")
            return False

        self.token = new_token
        exp = self._token_exp_ts()
        mins = int((exp - time.time()) / 60) if exp else "?"
        print(f"reauth OK — fresh token (exp in {mins} min)")

        if self.on_cookies_updated:
            try:
                self.on_cookies_updated(self.export_cookies())
            except Exception as e:
                print(f"cookie persist error: {e}")
        return True

    # ---------- data ----------

    def getRecentData(self):
        """Returns dict with glucose, trend, ts, pumpInfo, stats*, rawAgg1d. None on failure."""
        # Proactively reauth every cycle to keep the Auth0 session warm — it appears to
        # idle-expire, so we never let it go stale (reauth is cheap and rolls the session).
        self.reauth()

        r = self._fetch()
        if r is not None and r.status_code == 401:
            print("data fetch 401 — reauth + retry")
            if not self.reauth():
                return None
            r = self._fetch()

        if r is None:
            return None
        if r.status_code != 200:
            print(f"data fetch HTTP {r.status_code}: {r.text[:150]}")
            return None

        try:
            result = self._parse(r.json())
        except (KeyError, TypeError, ValueError) as e:
            print(f"parse error: {e} — {r.text[:200]}")
            return None

        if result is not None:
            result["patientName"] = self._fetch_user()
        return result

    def _fetch_user(self):
        """Patient display name (First Last) from /patient/users/me, cached."""
        if self._patient_name is not None:
            return self._patient_name or None
        try:
            r = self.session.get(USERS_ME_URL,
                                 headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
            if r.status_code == 200:
                me = r.json()
                fn = (me.get("firstName") or "").strip()
                ln = (me.get("lastName") or "").strip()
                self._patient_name = (fn + " " + ln).strip()
                print(f"patient: {self._patient_name or '(none)'}")
        except Exception as e:
            print(f"user fetch error: {e}")
            self._patient_name = ""  # cache the failure to avoid refetching every cycle
        return self._patient_name or None

    def _fetch(self):
        try:
            return self.session.get(
                DATA_URL,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=20,
            )
        except Exception as e:
            print(f"data fetch error: {e}")
            return None

    def _jwt_payload(self):
        try:
            b = self.token.split(".")[1]
            b += "=" * ((4 - len(b) % 4) % 4)
            return json.loads(base64.urlsafe_b64decode(b).decode())
        except Exception as e:
            print(f"PROBE: jwt decode error {e}")
            return {}

    def probe_realtime(self):
        """TEMP DIAGNOSTIC: does the live web token reach the carepartner
        real-time display/message endpoint? Logs results with 'PROBE:' prefix.
        Read-only; safe to run in production. Remove after diagnosis."""
        self.reauth()
        if not self.token:
            print("PROBE: no live token — cannot test")
            return
        H = {"Authorization": f"Bearer {self.token}", "Accept": "application/json",
             "Content-Type": "application/json"}

        # username must come from the token JWT (token_details.preferred_username)
        p = self._jwt_payload()
        td = p.get("token_details", {}) if isinstance(p.get("token_details"), dict) else {}
        print(f"PROBE: jwt keys={list(p.keys())[:15]} token_details keys={list(td.keys())[:15]}")
        u_candidates = [
            td.get("preferred_username"),
            p.get("preferred_username"),
            p.get("username"),
            p.get("sub"),
        ]
        u_candidates = [u for u in u_candidates if u]
        print(f"PROBE: username candidates={u_candidates}")

        ble_ep = "https://clcloud.minimed.eu/connect/carepartner/v6/display/message"
        try:
            s = self.session.get(f"{CARELINK_BASE}/patient/countries/settings?countryCode=il&language=en",
                                 headers=H, timeout=20)
            if s.status_code == 200:
                import re
                m = re.search(r'(https://[^"]*display/message)', s.text)
                if m:
                    ble_ep = m.group(1)
            print(f"PROBE: endpoint={ble_ep}")
        except Exception as e:
            print(f"PROBE: countries/settings error {e}")

        if not u_candidates:
            print("PROBE: no username in token — cannot build payload")
            return
        for u in dict.fromkeys(u_candidates):
            payload = {"username": u, "role": "patient"}
            try:
                r = self.session.post(ble_ep, headers=H, json=payload, timeout=25)
                print(f"PROBE: POST username={u} role=patient -> {r.status_code}")
                if r.status_code == 200:
                    j = r.json()
                    print(f"PROBE: TOPKEYS={sorted(j.keys())}")
                    for k in ("lastSG", "lastSGTrend", "lastAlarm", "sensorState",
                              "conduitSensorInRange", "reservoirRemainingUnits",
                              "reservoirLevelPercent", "medicalDeviceBatteryLevelPercent",
                              "activeInsulin", "sensorDurationHours", "timeToNextCalibHours",
                              "lastSensorTime", "averageSG", "belowHypoLimit", "aboveHyperLimit",
                              "systemStatusMessage", "conduitInRange", "gstBatteryLevel"):
                        if k in j:
                            print(f"PROBE:   {k} = {json.dumps(j[k])[:200]}")
                    sgs = j.get("sgs") or j.get("lastSGs")
                    if isinstance(sgs, list):
                        print(f"PROBE:   sgs len={len(sgs)} last={json.dumps(sgs[-1])[:200] if sgs else None}")
                    print("PROBE: *** REAL-TIME REACHED ***")
                    return
                else:
                    print(f"PROBE:   body={r.text[:200]}")
            except Exception as e:
                print(f"PROBE: POST username={u} error {e}")
        print("PROBE: done")

    def _parse(self, data):
        mgdl = data["ResponsePayload"]["mgdl"]

        latest_sg = None
        for day in mgdl.get("Agg1d", []):
            for reading in day.get("sg", {}).get("sgVal", []):
                if reading.get("sg", 0) > 0:
                    if latest_sg is None or reading.get("ts", 0) > latest_sg.get("ts", 0):
                        latest_sg = reading

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
        print(f"data OK — sg={result['glucose']} 7d TIR={s7.get('tirNormal')}% "
              f"avg={s7.get('avgSG')} TDD={s7.get('tdd')}u pump={pump_info.get('pumpModel')}")
        return result
