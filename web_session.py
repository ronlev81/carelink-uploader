"""
Web CareLink session capture + silent-renewal validation.

Two phases:
  capture — connect to the real Chrome running with --remote-debugging-port=9222
            (where you logged in + solved reCAPTCHA), grab all cookies + the
            auth_tmp_token, save to web_state.json
  renew   — using ONLY the saved cookies (no browser interaction), hit the
            sso/login URL and check whether a FRESH auth_tmp_token comes back
            WITHOUT hitting a login form / reCAPTCHA. This is exactly what
            Railway would do every few minutes.

Usage:
  .venv\\Scripts\\python.exe web_session.py capture
  .venv\\Scripts\\python.exe web_session.py renew
"""
import sys
import json
import time
import requests

LOGIN_URL = "https://carelink.minimed.eu/patient/sso/login?country=il&lang=en"
DATA_URL  = "https://clcloud.minimed.eu/connect/retina/v1/personalWebView"
STATE     = "web_state.json"
CDP       = "http://127.0.0.1:9222"
UA        = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def capture():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP)
        if not browser.contexts:
            print("ERROR: no browser context found over CDP")
            return
        ctx = browser.contexts[0]

        print("Waiting for login to complete (up to 4 min)...")
        tok = None
        for _ in range(240):
            cookies = ctx.cookies()
            t = next((c for c in cookies if c["name"] == "auth_tmp_token" and c.get("value")), None)
            if t:
                tok = t
                break
            time.sleep(1)

        cookies = ctx.cookies()
        with open(STATE, "w") as f:
            json.dump(cookies, f, indent=2)
        print(f"\nsaved {len(cookies)} cookies to {STATE}")
        print("cookie names:", sorted(set(c["name"] for c in cookies)))
        print("auth_tmp_token captured:", bool(tok))
        if tok:
            print("token1:", tok["value"][:30], "...")
        else:
            print(">>> No auth_tmp_token yet — did the login finish?")


def _requests_session():
    cookies = json.load(open(STATE))
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    for c in cookies:
        try:
            s.cookies.set(c["name"], c["value"],
                          domain=c["domain"].lstrip("."), path=c.get("path", "/"))
        except Exception:
            pass
    return s, cookies


def renew():
    s, cookies = _requests_session()
    old_tok = next((c["value"] for c in cookies if c["name"] == "auth_tmp_token"), None)
    print("old auth_tmp_token:", (old_tok or "")[:30])

    print(f"\n--- requests renewal: GET {LOGIN_URL}")
    r = s.get(LOGIN_URL, allow_redirects=True, timeout=30)
    print("final url:", r.url)
    print("status   :", r.status_code)
    hit_login = ("carelink-login" in r.url) or ("/u/login" in r.url)
    print("hit login form / reCAPTCHA page?:", hit_login)

    new_tok = s.cookies.get("auth_tmp_token")
    print("fresh auth_tmp_token via requests:", bool(new_tok))
    if new_tok:
        print("token2:", new_tok[:30])
        print("token changed vs old:", new_tok != old_tok)
        rd = s.get(DATA_URL, headers={"Authorization": f"Bearer {new_tok}"}, timeout=30)
        print("data API with fresh token -> HTTP", rd.status_code,
              "(200 = renewal WORKS end-to-end)")
        return

    print("\n>>> requests couldn't complete silent renewal (likely needs JS).")
    print(">>> Trying headless browser renewal with the same cookies...")
    renew_headless(cookies, old_tok)


def renew_headless(cookies, old_tok):
    from playwright.sync_api import sync_playwright
    pw_cookies = []
    for c in cookies:
        pw_cookies.append({
            "name": c["name"], "value": c["value"],
            "domain": c["domain"], "path": c.get("path", "/"),
        })
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA)
        try:
            ctx.add_cookies(pw_cookies)
        except Exception as e:
            print("add_cookies error:", e)
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        url = page.url
        for _ in range(20):
            page.wait_for_timeout(1000)
            url = page.url
            if "carelink-login" not in url and "carelink.minimed.eu" in url:
                break
        hit_login = ("carelink-login" in url) or ("/u/login" in url)
        tok = next((c["value"] for c in ctx.cookies() if c["name"] == "auth_tmp_token"), None)
        print("headless final url:", url)
        print("hit login form / reCAPTCHA?:", hit_login)
        print("fresh auth_tmp_token via headless:", bool(tok))
        if tok:
            print("token2:", tok[:30], "| changed:", tok != old_tok)
        browser.close()


def renew_headless_cmd():
    cookies = json.load(open(STATE))
    old_tok = next((c["value"] for c in cookies if c["name"] == "auth_tmp_token"), None)
    renew_headless(cookies, old_tok)


def investigate():
    """Attach to an already-logged-in Chrome and trace the SPA's token-refresh traffic."""
    from playwright.sync_api import sync_playwright
    KW = ["authorize", "/token", "oauth", "reauth", "checksession", "prompt=none",
          "/sso/", "session", "/connect/", "renew", "refresh", "silent", "/api/"]
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        page = ctx.pages[-1] if ctx.pages else ctx.new_page()

        tok0 = next((c for c in ctx.cookies() if c["name"] == "auth_tmp_token" and c.get("value")), None)
        if not tok0:
            print(">>> not logged in (no auth_tmp_token). Log in first, then rerun.")
            return
        print("logged in. current url:", page.url)
        print("auth_tmp_token (token1):", tok0["value"][:30])
        with open(STATE, "w") as f:
            json.dump(ctx.cookies(), f, indent=2)
        print("saved fresh cookies to", STATE)

        hits = []

        def on_request(req):
            u = req.url
            if any(k in u.lower() for k in KW):
                hits.append(f"{req.method} {u}")

        page.on("request", on_request)
        for fr in page.frames:
            pass

        print("\n=== reloading app to capture silent refresh traffic ===")
        try:
            page.goto(page.url, wait_until="networkidle", timeout=60000)
        except Exception as e:
            print("reload note:", e)
        page.wait_for_timeout(6000)

        print(f"\nauth/api requests during reload ({len(hits)}):")
        for h in hits:
            print("  ", h[:220])

        tok1 = next((c["value"] for c in ctx.cookies() if c["name"] == "auth_tmp_token"), None)
        print("\ntoken after reload:", (tok1 or "")[:30], "| changed:", bool(tok1) and tok1 != tok0["value"])


def renew_cdp():
    """Renew inside the real logged-in Chrome (full session) — the decisive test."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        before = next((c["value"] for c in ctx.cookies() if c["name"] == "auth_tmp_token"), None)
        print("token before:", (before or "")[:30])

        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)

        # Poll up to 25s for the flow to settle
        final_url = page.url
        for _ in range(25):
            page.wait_for_timeout(1000)
            final_url = page.url
            if "carelink-login" not in final_url and "carelink.minimed.eu" in final_url:
                break

        hit_login = ("carelink-login" in final_url) or ("/u/login" in final_url)
        after = next((c["value"] for c in ctx.cookies() if c["name"] == "auth_tmp_token"), None)
        print("final url :", final_url)
        print("hit login form / reCAPTCHA?:", hit_login)
        print("token after:", (after or "")[:30])
        print("token CHANGED (renewed silently):", bool(after) and after != before)
        try:
            page.close()
        except Exception:
            pass


def _decode_exp(token):
    import base64 as b64
    try:
        p = token.split(".")[1]
        p += "=" * ((4 - len(p) % 4) % 4)
        payload = json.loads(b64.urlsafe_b64decode(p).decode())
        exp = payload.get("exp")
        if exp:
            import datetime
            mins = (exp - time.time()) / 60
            return f"exp in {mins:.0f} min"
    except Exception as e:
        return f"decode err: {e}"
    return "?"


def forcerefresh():
    """Delete auth_tmp_token (keep Auth0 session), reload, see how the SPA re-mints the token."""
    from playwright.sync_api import sync_playwright
    KW = ["authorize", "/token", "oauth", "reauth", "checksession", "prompt=none",
          "/sso/", "session", "/connect/", "renew", "refresh", "silent", "/api/"]
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        page = ctx.pages[-1] if ctx.pages else ctx.new_page()
        tok0 = next((c for c in ctx.cookies() if c["name"] == "auth_tmp_token" and c.get("value")), None)
        if not tok0:
            print(">>> not logged in. Log in first.")
            return
        print("token1:", tok0["value"][:25], "|", _decode_exp(tok0["value"]))

        # Delete ONLY the auth_tmp_token, keep the Auth0 session cookies
        try:
            ctx.clear_cookies(name="auth_tmp_token")
        except TypeError:
            print("(filtered clear_cookies unsupported; clearing carelink.minimed.eu cookies)")
            ctx.clear_cookies(domain="carelink.minimed.eu")
        still = next((c for c in ctx.cookies() if c["name"] == "auth_tmp_token"), None)
        print("auth_tmp_token after delete:", bool(still))

        hits = []
        def on_request(req):
            u = req.url
            if any(k in u.lower() for k in KW):
                hits.append(f"{req.method} {u}")
        def on_response(resp):
            try:
                sc = resp.headers.get("set-cookie", "")
                if "auth_tmp_token" in sc:
                    hits.append(f"<<SET auth_tmp_token>> via {resp.status} {resp.url[:120]}")
            except Exception:
                pass
        page.on("request", on_request)
        page.on("response", on_response)

        print("\n=== navigating to dashboard with NO token (session only) ===")
        try:
            page.goto("https://carelink.minimed.eu/app/dashboard", wait_until="networkidle", timeout=60000)
        except Exception as e:
            print("nav note:", e)
        # poll for a new token / settle
        new = None
        for _ in range(20):
            page.wait_for_timeout(1000)
            new = next((c["value"] for c in ctx.cookies() if c["name"] == "auth_tmp_token" and c.get("value")), None)
            if new:
                break
        final = page.url
        hit_login = ("carelink-login" in final) or ("/u/login" in final)

        print(f"\nrequests during recovery ({len(hits)}):")
        for h in hits:
            print("  ", h[:220])
        print("\nfinal url:", final)
        print("hit login form / reCAPTCHA?:", hit_login)
        print("NEW auth_tmp_token minted silently:", bool(new), "| changed:", bool(new) and (not tok0 or new != tok0["value"]))
        if new:
            print("token2:", new[:25], "|", _decode_exp(new))


def reauth():
    """Test the /patient/sso/reauth endpoint — silent token refresh using existing session."""
    cookies = json.load(open(STATE))
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
    for c in cookies:
        try:
            s.cookies.set(c["name"], c["value"], domain=c["domain"].lstrip("."), path=c.get("path", "/"))
        except Exception:
            pass
    old = next((c["value"] for c in cookies if c["name"] == "auth_tmp_token"), None)
    print("old token:", (old or "")[:25], "|", _decode_exp(old) if old else "(none)")

    REAUTH = "https://carelink.minimed.eu/patient/sso/reauth"

    def extract_new(resp):
        # 1) cookie jar
        nt = s.cookies.get("auth_tmp_token")
        if nt and nt != old:
            return nt, "cookie"
        # 2) JSON body
        try:
            j = resp.json()
            for k in ("auth_tmp_token", "token", "accessToken", "access_token"):
                if isinstance(j, dict) and j.get(k):
                    return j[k], f"body.{k}"
        except Exception:
            pass
        return None, None

    for method in ["GET", "POST"]:
        hdrs = {}
        if old:
            hdrs["Authorization"] = f"Bearer {old}"
        try:
            r = s.request(method, REAUTH, headers=hdrs, allow_redirects=True, timeout=30)
        except Exception as e:
            print(f"{method} error: {e}")
            continue
        hit_login = ("carelink-login" in r.url) or ("/u/login" in r.url)
        print(f"\n{method} reauth -> {r.status_code}  final={r.url[:80]}  loginform={hit_login}")
        print("  set-cookie auth_tmp_token:", "auth_tmp_token" in r.headers.get("set-cookie", ""))
        print("  body:", r.text[:160].replace("\n", " "))
        new, src = extract_new(r)
        if new:
            print(f"  >>> NEW TOKEN via {method} ({src}):", new[:25], "|", _decode_exp(new))
            rd = s.get(DATA_URL, headers={"Authorization": f"Bearer {new}"}, timeout=30)
            print("  data API with NEW token ->", rd.status_code, "(200 = SILENT REFRESH WORKS)")
            return

    print("\n--- token/status endpoint ---")
    try:
        rs = s.get("https://carelink.minimed.eu/api/uploader/v1/patient/token/status",
                   headers={"Authorization": f"Bearer {old}"} if old else {}, timeout=30)
        print("token/status ->", rs.status_code, "|", rs.text[:200])
    except Exception as e:
        print("token/status error:", e)


def freshness():
    """Inspect how fresh the personalWebView glucose data is (latest reading age + cadence)."""
    import datetime
    from carelink_client import CareLinkClient, DATA_URL
    cookies = json.load(open(STATE))
    client = CareLinkClient(cookies=cookies)
    if client._needs_reauth():
        client.reauth()
    r = client.session.get(DATA_URL, headers={"Authorization": f"Bearer {client.token}"}, timeout=20)
    data = r.json()
    mgdl = data["ResponsePayload"]["mgdl"]
    allr = []
    for day in mgdl.get("Agg1d", []):
        for reading in day.get("sg", {}).get("sgVal", []):
            if reading.get("sg", 0) > 0:
                allr.append((reading["ts"], reading["sg"]))
    allr.sort()
    print(f"total valid readings in Agg1d: {len(allr)}")
    print("last 8 readings:")
    for ts, sg in allr[-8:]:
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%m-%d %H:%M UTC")
        print(f"  {dt}  sg={sg}")
    if allr:
        age_min = (time.time() - allr[-1][0]) / 60
        print(f"\nLATEST reading is {age_min:.0f} min old  (sg={allr[-1][1]})")
        print("→ if this is >15 min, personalWebView lags / isn't real-time")


def reauthloop():
    """Chain several reauths on ONE session to classify the failure: single-use vs time-limited."""
    from carelink_client import CareLinkClient
    cookies = json.load(open(STATE))
    client = CareLinkClient(cookies=cookies)
    for i in range(5):
        ok = client.reauth()
        exp = client._token_exp_ts()
        mins = int((exp - time.time()) / 60) if exp else "?"
        print(f"reauth #{i + 1}: {'OK (token exp ' + str(mins) + ' min)' if ok else 'FAIL (401)'}")
        if i < 4:
            time.sleep(5)


def export():
    """Print the cookie jar as a compact JSON for the Railway COOKIE_JAR env var."""
    cookies = json.load(open(STATE))
    # keep only what we need (name/value/domain/path)
    slim = [{"name": c["name"], "value": c["value"],
             "domain": c.get("domain", ""), "path": c.get("path", "/")} for c in cookies]
    compact = json.dumps(slim, separators=(",", ":"))
    with open("cookie_jar.txt", "w", encoding="utf-8") as f:
        f.write(compact)
    print(f"Wrote cookie_jar.txt ({len(slim)} cookies, {len(compact)} chars).")
    print("Open cookie_jar.txt, copy the ENTIRE line, and paste it as the")
    print("Railway env var  COOKIE_JAR.  Keep it secret (it's your live session).")


def monitor():
    """POST /patient/v2/monitor/data and dump all device-status fields (PII excluded)."""
    from carelink_client import CareLinkClient
    cookies = json.load(open(STATE))
    client = CareLinkClient(cookies=cookies)
    tok = client.token
    url = "https://carelink.minimed.eu/patient/v2/monitor/data"
    PII = ["name", "first", "last", "email", "username", "address", "phone", "birth",
           "dob", "nick", "city", "street", "zip", "postal"]

    def walk(node, path, out):
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{path}.{k}"
                if isinstance(v, (int, float, bool, str)) and v not in (None, ""):
                    if not any(pw in k.lower() for pw in PII):
                        out.append((p, v))
                else:
                    walk(v, p, out)
        elif isinstance(node, list):
            print(f"    {path}: list[{len(node)}]")
            if node:
                walk(node[0], path + "[0]", out)

    # Find the patient's username — check the JWT claims and /users/me
    import base64 as _b64

    def jwt_claims(t):
        try:
            p = t.split(".")[1]; p += "=" * ((4 - len(p) % 4) % 4)
            return json.loads(_b64.urlsafe_b64decode(p).decode())
        except Exception:
            return {}

    claims = jwt_claims(tok)
    print("token claim keys:", list(claims.keys()))
    td = claims.get("token_details") if isinstance(claims.get("token_details"), dict) else {}
    if td:
        print("token_details keys:", list(td.keys()))
    me = client.session.get("https://carelink.minimed.eu/patient/users/me",
                            headers={"Authorization": f"Bearer {tok}"}, timeout=20).json()

    cands, seen = [], set()
    for src in (td, claims, me if isinstance(me, dict) else {}):
        for k in ("preferred_username", "username", "userName", "login", "loginName", "sub", "patientUsername"):
            v = src.get(k)
            if isinstance(v, str) and v and v not in seen:
                seen.add(v); cands.append((k, v))
    print("candidate usernames:", [(k, v[:3] + '***') for k, v in cands])

    for k, uname in cands:
        kw = {"headers": {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
              "data": json.dumps({"patientUsername": uname}), "timeout": 20}
        r = client.session.post(url, **kw)
        print(f"\nPOST monitor/data  patientUsername<-{k}  -> {r.status_code}")
        if r.status_code != 200:
            print("   ", r.text[:160].replace("\n", " "))
            continue
        try:
            data = r.json()
        except Exception:
            print("    non-json:", r.text[:150])
            continue
        if isinstance(data, dict):
            print("    top keys:", list(data.keys()))
        out = []
        walk(data, "M", out)
        print(f"    --- all scalar fields ({len(out)}, PII removed) ---")
        for p, v in out:
            print(f"      {p} = {str(v)[:60]}")
        break


def probe():
    """Probe candidate web endpoints for live device status (reservoir/battery/sensor-days)."""
    from carelink_client import CareLinkClient
    cookies = json.load(open(STATE))
    client = CareLinkClient(cookies=cookies)
    if client._needs_reauth():
        client.reauth()  # best effort; old token may still work for GETs
    tok = client.token
    base = "https://carelink.minimed.eu"
    me = client.session.get("https://carelink.minimed.eu/patient/users/me",
                            headers={"Authorization": f"Bearer {tok}"}, timeout=20).json()
    print("role:", me.get("role"), "| cpRegistrationStatus:", me.get("cpRegistrationStatus"))
    endpoints = [
        ("GET", "/patient/m2m/links/patients"),
        ("GET", "/patient/data/sharing/display"),
        ("GET", "/patient/links/requests/latest"),
        ("GET", "/patient/users/me/RecentActivity"),
        ("GET", "/patient/last/device"),
    ]
    KW = ["reservoir", "batter", "sensor", "insulin", "active", "bolus", "basal",
          "conduit", "medical", "pump", "duration", "remaining", "expir", "calibrat",
          "shield", "soc", "lifetime", "days", "hours", "minutes", "level", "transmitter",
          "lastSG", "bgUnits", "timeToNextCalib", "sgBelowLimit"]
    PII = ["name", "first", "last", "email", "username", "address", "phone", "birth",
           "dob", "nick", "city", "street"]

    def walk(node, path, out):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = k.lower()
                p = f"{path}.{k}"
                if isinstance(v, (int, float, bool, str)) and v not in (None, ""):
                    if any(w in kl for w in KW) and not any(pw in kl for pw in PII):
                        out.append((p, v))
                else:
                    walk(v, p, out)
        elif isinstance(node, list) and node:
            walk(node[0], path + "[0]", out)

    for method, ep in endpoints:
        try:
            r = client.session.request(method, base + ep,
                                       headers={"Authorization": f"Bearer {tok}"}, timeout=20)
        except Exception as e:
            print(f"\n{method} {ep} -> ERROR {e}")
            continue
        print(f"\n{method} {ep} -> {r.status_code}")
        if r.status_code != 200:
            print("   ", r.text[:120].replace("\n", " "))
            continue
        try:
            data = r.json()
        except Exception:
            print("    non-json:", r.text[:120])
            continue
        if isinstance(data, dict):
            print("    top keys:", list(data.keys())[:25])
        out = []
        walk(data, ep.split("/")[-1], out)
        if out:
            print("    >>> device-status fields:")
            for p, v in out[:50]:
                print(f"       {p} = {str(v)[:55]}")
        else:
            print("    (no device-status fields matched)")


def dumpdata():
    """Fetch personalWebView and reveal what pump/sensor status fields exist (no PII)."""
    from carelink_client import CareLinkClient, DATA_URL
    cookies = json.load(open(STATE))
    client = CareLinkClient(cookies=cookies)
    if client._needs_reauth():
        client.reauth()
    r = client.session.get(DATA_URL, headers={"Authorization": f"Bearer {client.token}"}, timeout=20)
    print("HTTP", r.status_code)
    data = r.json()
    rp = data.get("ResponsePayload", data)

    print("\n=== ResponsePayload top-level keys ===")
    for k, v in rp.items():
        t = type(v).__name__
        size = f"[{len(v)}]" if isinstance(v, (list, dict)) else ""
        print(f"  {k}: {t}{size}")

    # Recursively hunt for device-status fields (numbers/strings, not PII)
    KW = ["reservoir", "battery", "sensor", "insulin", "active", "bolus", "basal",
          "conduit", "medical", "pump", "calibrat", "duration", "remaining", "expir",
          "time", "lastSG", "sg", "transmitter", "warmup", "wear", "status", "level",
          "lifetime", "minutes", "hours", "days", "shield"]
    PII = ["name", "first", "last", "email", "patient", "username", "address", "phone", "birth", "dob"]
    found = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = k.lower()
                p = f"{path}.{k}"
                if isinstance(v, (int, float, bool, str)) and v not in (None, ""):
                    if any(w in kl for w in KW) and not any(pw in kl for pw in PII):
                        found.append((p, v))
                else:
                    walk(v, p)
        elif isinstance(node, list) and node:
            walk(node[0], path + "[0]")  # sample first element only

    walk(rp, "RP")
    print(f"\n=== device-status candidate fields ({len(found)}) ===")
    for p, v in found:
        sv = str(v)[:60]
        print(f"  {p} = {sv}")


def testclient():
    """End-to-end test of the refactored CareLinkClient using web_state.json cookies."""
    from carelink_client import CareLinkClient
    cookies = json.load(open(STATE))

    def persisted(c):
        print(f"  [callback] would persist {len(c)} cookies to Firestore")

    client = CareLinkClient(cookies=cookies, on_cookies_updated=persisted)
    print("needs_reauth:", client._needs_reauth())
    data = client.getRecentData()
    if not data:
        print(">>> getRecentData returned None")
        return
    print("\n=== DATA ===")
    print("glucose :", data["glucose"], "trend:", data["trend"])
    print("pumpInfo:", data["pumpInfo"])
    print("7d      :", data["stats7d"])
    print("today   :", {k: data["statsToday"].get(k) for k in ("tirNormal", "avgSG", "tdd", "autoMode")})


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "capture"
    {"capture": capture, "renew": renew, "renewcdp": renew_cdp,
     "renewheadless": renew_headless_cmd, "investigate": investigate,
     "forcerefresh": forcerefresh, "reauth": reauth,
     "testclient": testclient, "export": export, "dumpdata": dumpdata,
     "probe": probe, "monitor": monitor, "reauthloop": reauthloop,
     "freshness": freshness}.get(cmd, capture)()
