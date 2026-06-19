"""
CareLink EU client.
Glucose endpoint: GET https://clcloud.minimed.eu/connect/retina/v1/personalWebView
Auth: Authorization: Bearer <CARELINK_TOKEN>
"""
import os
import requests

CLCLOUD_BASE = "https://clcloud.minimed.eu"


def _extract_agg_stats(agg):
    """Pull TIR, avg SG, TDD, autoMode from a single Agg block."""
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
        """
        Returns dict with:
          - glucose (int mg/dL), ts (unix seconds), trend (str)
          - statsToday, statsYday, stats7d, stats14d, stats30d: each has
            tirNormal/High/Low/ExtHigh/ExtLow (%), avgSG (mg/dL),
            sdSG, tdd (units), autoMode (%), sensorUsage (%)
        Returns None on failure.
        """
        r = self.session.get(
            f"{CLCLOUD_BASE}/connect/retina/v1/personalWebView",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        print(f"Data fetch: {r.status_code}")

        if r.status_code == 401:
            print("Token expired — update CARELINK_TOKEN in Railway")
            return None
        if r.status_code != 200:
            print(f"Unexpected status: {r.status_code} — {r.text[:200]}")
            return None

        try:
            data = r.json()
            mgdl = data["ResponsePayload"]["mgdl"]

            # Latest glucose reading across all Agg1d days
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

            # Per-period aggregate stats
            agg1d          = mgdl.get("Agg1d", [])
            today_agg      = next((d for d in agg1d if d.get("lastInProgressDay")), agg1d[0] if agg1d else None)
            yesterday_agg  = next((d for d in agg1d if not d.get("lastInProgressDay")), None)
            agg7d_list     = mgdl.get("Agg7d",  [])
            agg14d_list    = mgdl.get("Agg14d", [])
            agg30d_list    = mgdl.get("Agg30d", [])

            # Pump device metadata from top-level ResponsePayload
            rp = data.get("ResponsePayload", {})
            dd = rp.get("deviceDetails", {})
            pump_info = {
                "pumpModel":   dd.get("deviceModel"),
                "sensorModel": dd.get("sensorModel"),
            }
            pump_info = {k: v for k, v in pump_info.items() if v is not None}
            print(f"Pump: {pump_info.get('pumpModel')} sensor: {pump_info.get('sensorModel')}")

            result = {
                "glucose":    latest_sg.get("sg")            if latest_sg else None,
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
