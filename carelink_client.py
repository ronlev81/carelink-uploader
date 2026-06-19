"""
CareLink EU client — implements the Auth0 PKCE + Janrain flow used by Medtronic.
Based on reverse-engineering of the carelink.minimed.eu web app.
"""
import base64
import hashlib
import os
import re
import time
import requests
from urllib.parse import urlparse, parse_qs, urlencode

CARELINK_EU_BASE   = "https://carelink.minimed.eu"
AUTH0_DOMAIN       = "carelink-login.minimed.eu"
AUTH0_CLIENT_ID    = "MpsGIvoIfjwGdX7LhxQ6THhCTMMxKQNU9"


class CareLinkClient:
    def __init__(self, username, password, country="eu"):
        self.username = username
        self.password = password
        self.country  = country
        self.session  = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

    # ------------------------------------------------------------------ #
    #  PKCE helpers                                                         #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def _pkce_pair(self):
        verifier  = self._b64url(os.urandom(32))
        challenge = self._b64url(hashlib.sha256(verifier.encode()).digest())
        return verifier, challenge

    # ------------------------------------------------------------------ #
    #  Login                                                                #
    # ------------------------------------------------------------------ #
    def login(self) -> bool:
        print("Starting Auth0 PKCE login...")
        verifier, challenge = self._pkce_pair()

        # 1. Initiate Auth0 authorisation
        auth_params = {
            "client_id":             AUTH0_CLIENT_ID,
            "response_type":         "code",
            "redirect_uri":          f"{CARELINK_EU_BASE}/patient/sso/callback",
            "scope":                 "openid email profile",
            "audience":              "https://carelink.minimed.eu/patient",
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
            "ui_locales":            "en",
        }
        r = self.session.get(
            f"https://{AUTH0_DOMAIN}/authorize",
            params=auth_params,
            allow_redirects=True,
        )
        print(f"Auth0 authorize: {r.status_code} → {r.url[:80]}")
        if r.status_code != 200:
            return False

        # 2. Parse the login form and submit credentials
        state_match = re.search(r'name="state"\s+value="([^"]+)"', r.text)
        state = state_match.group(1) if state_match else parse_qs(urlparse(r.url).query).get("state", [""])[0]

        form_data = {
            "username": self.username,
            "password": self.password,
            "action":   "default",
            "state":    state,
        }
        r2 = self.session.post(
            r.url,
            data=form_data,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Referer": r.url},
            allow_redirects=True,
        )
        print(f"Credentials POST: {r2.status_code} → {r2.url[:80]}")

        if "Wrong email or password" in r2.text or "error" in r2.url.lower():
            print("ERROR: wrong credentials")
            return False

        # 3. Extract the auth code from the callback URL
        callback_url = r2.url
        qs = parse_qs(urlparse(callback_url).query)
        code = qs.get("code", [None])[0]
        if not code:
            # Maybe we need to follow one more redirect
            for resp in r2.history + [r2]:
                code = parse_qs(urlparse(resp.url).query).get("code", [None])[0]
                if code:
                    break
        print(f"Auth code: {'OK' if code else 'MISSING'}")
        if not code:
            return False

        # 4. Exchange code for tokens
        token_resp = self.session.post(
            f"https://{AUTH0_DOMAIN}/oauth/token",
            json={
                "grant_type":    "authorization_code",
                "client_id":     AUTH0_CLIENT_ID,
                "code":          code,
                "redirect_uri":  f"{CARELINK_EU_BASE}/patient/sso/callback",
                "code_verifier": verifier,
            },
        )
        print(f"Token exchange: {token_resp.status_code}")
        if token_resp.status_code != 200:
            print(token_resp.text[:300])
            return False

        tokens = token_resp.json()
        self._access_token = tokens.get("access_token", "")
        print("Login successful")
        return bool(self._access_token)

    # ------------------------------------------------------------------ #
    #  Data                                                                 #
    # ------------------------------------------------------------------ #
    def getRecentData(self):
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
        params = {
            "cpSerialNumber": "NONE",
            "msgType":        "last24hours",
            "requestTime":    int(time.time() * 1000),
        }
        r = self.session.get(
            f"{CARELINK_EU_BASE}/patient/connect/data",
            headers=headers,
            params=params,
        )
        print(f"Data fetch: {r.status_code}")
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                print(f"Non-JSON: {r.text[:200]}")
        elif r.status_code == 401:
            print("Token expired — re-login needed")
        return None
