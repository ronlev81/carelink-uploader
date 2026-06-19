"""
CareLink EU client.
Auth strategy:
  1. GET /patient/sso/login → follows to Auth0 /authorize URL → extract client_id + state
  2. POST /co/authenticate (Auth0 cross-origin endpoint) → login_ticket
  3. GET /authorize?login_ticket=... → redirects to CareLink callback → session cookie
  4. GET /patient/connect/data with session cookie
"""
import re
import time
import json
import requests
from urllib.parse import urlparse, parse_qs, urlencode

CARELINK_EU_BASE = "https://carelink.minimed.eu"
AUTH0_DOMAIN     = "carelink-login.minimed.eu"


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
        })

    # ------------------------------------------------------------------
    def _sso_init(self):
        """Return (client_id, redirect_uri, state, auth_url) from SSO redirect."""
        r = self.session.get(
            f"{CARELINK_EU_BASE}/patient/sso/login",
            params={"country": self.country, "lang": "en"},
            allow_redirects=True,
        )
        print(f"SSO init: {r.status_code} → {r.url[:100]}")
        parsed = urlparse(r.url)
        qs     = parse_qs(parsed.query)

        # Try URL params first (older Auth0 /authorize redirect)
        client_id    = qs.get("client_id",    [None])[0]
        redirect_uri = qs.get("redirect_uri", [None])[0]
        state        = qs.get("state",        [None])[0]

        # Auth0 "New Universal Login" hides client_id inside the page HTML
        if not client_id:
            for pattern in [
                r'"clientID"\s*:\s*"([^"]+)"',
                r'"client_id"\s*:\s*"([^"]+)"',
                r'clientId["\s:=]+([A-Za-z0-9_\-]{10,})',
            ]:
                m = re.search(pattern, r.text)
                if m:
                    client_id = m.group(1)
                    break

        if not redirect_uri:
            m = re.search(r'"redirectURI"\s*:\s*"([^"]+)"', r.text)
            if m:
                redirect_uri = m.group(1).replace("\\u002F", "/")

        print(f"  client_id={client_id}  state={state and state[:20]}")
        if not client_id:
            # Search full HTML for any occurrence of clientID / client_id
            for pat in [r'"clientID"', r'"client_id"', r'clientId', r'"CLIENT_ID"']:
                idx = r.text.find(pat.strip('"'))
                if idx >= 0:
                    print(f"DEBUG found '{pat}' at index {idx}:")
                    print(r.text[max(0,idx-50):idx+200])
                    break
            else:
                print("DEBUG: no clientID found in HTML at all")
                print("DEBUG FULL HTML LENGTH:", len(r.text))
                # Print a chunk around the word "auth" to find config block
                idx = r.text.lower().find('"auth0"')
                if idx < 0:
                    idx = r.text.lower().find('auth0config')
                if idx >= 0:
                    print(r.text[max(0,idx-100):idx+400])
        return client_id, redirect_uri, state, r.url

    # ------------------------------------------------------------------
    def _co_authenticate(self, client_id):
        """Auth0 cross-origin authenticate — returns login_ticket."""
        payload = {
            "client_id":       client_id,
            "username":        self.username,
            "password":        self.password,
            "credential_type": "http://auth0.com/oauth/grant-type/password-realm",
            "realm":           "Username-Password-Authentication",
        }
        r = self.session.post(
            f"https://{AUTH0_DOMAIN}/co/authenticate",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Origin":       f"https://{AUTH0_DOMAIN}",
            },
        )
        print(f"co/authenticate: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            ticket = data.get("login_ticket")
            print(f"  login_ticket={'OK' if ticket else 'MISSING'}")
            return ticket
        print(f"  body: {r.text[:300]}")
        return None

    # ------------------------------------------------------------------
    def _exchange_ticket(self, client_id, redirect_uri, login_ticket):
        """GET /authorize with login_ticket → follow back to CareLink."""
        params = {
            "client_id":     client_id,
            "response_type": "code",
            "redirect_uri":  redirect_uri,
            "scope":         "openid email profile",
            "login_ticket":  login_ticket,
            "prompt":        "none",
        }
        r = self.session.get(
            f"https://{AUTH0_DOMAIN}/authorize",
            params=params,
            allow_redirects=True,
        )
        print(f"ticket exchange: {r.status_code} → {r.url[:100]}")
        return r

    # ------------------------------------------------------------------
    def login(self) -> bool:
        client_id, redirect_uri, state, auth_url = self._sso_init()
        if not client_id:
            print("Could not extract client_id from SSO redirect")
            return False

        ticket = self._co_authenticate(client_id)
        if not ticket:
            print("co/authenticate failed")
            return False

        r = self._exchange_ticket(client_id, redirect_uri, ticket)

        cookies = list(self.session.cookies.keys())
        print(f"cookies after exchange: {cookies}")

        # Check we landed back on CareLink with a real session
        if CARELINK_EU_BASE in r.url or any(
            k.lower() in ("auth", "session", "jwt", "access_token", "carelink")
            for k in cookies
        ):
            print("Login OK")
            return True

        # Fallback: try to retrieve a known protected page to confirm
        test = self.session.get(f"{CARELINK_EU_BASE}/patient/dashboard", allow_redirects=False)
        print(f"Dashboard probe: {test.status_code}")
        if test.status_code in (200, 302) and CARELINK_EU_BASE in (test.headers.get("Location", "")):
            print("Login OK (redirect to dashboard)")
            return True

        print("Login FAILED — no CareLink session cookie")
        return False

    # ------------------------------------------------------------------
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
            print(f"  body: {r.text[:200]}")
        return None
