"""
CareLink EU client — goes through carelink.minimed.eu/patient/sso/login
which redirects to Auth0, then submits credentials to the form action URL.
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
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        })

    def _parse_form(self, html, base_url):
        """Return (action_url, hidden_fields) from the first <form> in html."""
        form_match = re.search(r'<form[^>]*action=["\']([^"\']+)["\'][^>]*>(.*?)</form>',
                               html, re.DOTALL | re.IGNORECASE)
        action = base_url
        fields = {}
        if form_match:
            action = form_match.group(1)
            if action.startswith('/'):
                parsed = urlparse(base_url)
                action = f"{parsed.scheme}://{parsed.netloc}{action}"
            for m in re.finditer(r'<input[^>]+>', form_match.group(2), re.IGNORECASE):
                tag = m.group(0)
                itype = re.search(r'type=["\']([^"\']+)["\']', tag)
                name  = re.search(r'name=["\']([^"\']+)["\']', tag)
                value = re.search(r'value=["\']([^"\']*)["\']', tag)
                if name and (not itype or itype.group(1).lower() != 'submit'):
                    fields[name.group(1)] = value.group(1) if value else ''
        return action, fields

    def login(self) -> bool:
        print("Step 1: CareLink SSO → Auth0")
        r1 = self.session.get(
            f"{CARELINK_EU_BASE}/patient/sso/login",
            params={"country": self.country, "lang": "en"},
            allow_redirects=True,
        )
        print(f"  {r1.status_code} → {r1.url[:90]}")
        if r1.status_code != 200:
            return False

        print("Step 2: parse Auth0 form")
        action, fields = self._parse_form(r1.text, r1.url)
        print(f"  action: {action[:80]}")
        print(f"  fields: {list(fields.keys())}")

        # Fill credentials — Auth0 Universal Login field names
        for key in ("username", "email", "login"):
            if key in fields or not fields:
                fields["username"] = self.username
                break
        fields["username"] = self.username
        fields["password"] = self.password
        # Remove captcha — empty value causes 400; omit entirely
        fields.pop("captcha", None)
        if "action" not in fields:
            fields["action"] = "default"

        print("Step 3: POST credentials")
        r2 = self.session.post(
            action,
            data=fields,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": r1.url,
                "Origin": f"https://{urlparse(r1.url).netloc}",
            },
            allow_redirects=True,
        )
        print(f"  {r2.status_code} → {r2.url[:90]}")

        if "Wrong email or password" in r2.text:
            print("  ERROR: wrong credentials")
            return False

        # Step 4: follow back to CareLink to get session cookie
        if CARELINK_EU_BASE not in r2.url:
            r3 = self.session.get(
                f"{CARELINK_EU_BASE}/patient/sso/login",
                params={"country": self.country, "lang": "en"},
                allow_redirects=True,
            )
            print(f"Step 4 session: {r3.status_code}")

        cookies = list(self.session.cookies.keys())
        print(f"  cookies: {cookies}")
        # Success if we have a session/access cookie from carelink
        return any("carelink" in c.lower() or "access" in c.lower()
                   or "session" in c.lower() or "jwt" in c.lower()
                   for c in cookies) or len(cookies) > 2

    def getRecentData(self):
        params = {
            "cpSerialNumber": "NONE",
            "msgType":        "last24hours",
            "requestTime":    int(time.time() * 1000),
        }
        r = self.session.get(
            f"{CARELINK_EU_BASE}/patient/connect/data",
            params=params,
        )
        print(f"Data fetch: {r.status_code}")
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                print(f"Non-JSON: {r.text[:300]}")
        return None
