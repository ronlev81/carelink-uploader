import os
import re
import time
import hashlib
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

CARELINK_USERNAME = os.environ['CARELINK_USERNAME']
CARELINK_PASSWORD = os.environ['CARELINK_PASSWORD']
CARELINK_COUNTRY  = os.environ.get('CARELINK_COUNTRY', 'il')
NS_HOST           = os.environ['NS_HOST'].rstrip('/')
API_SECRET        = os.environ['API_SECRET']
INTERVAL          = int(os.environ.get('UPLOAD_INTERVAL', '300'))

API_SECRET_HASH = hashlib.sha1(API_SECRET.encode()).hexdigest()
NS_HEADERS = {'API-SECRET': API_SECRET_HASH, 'Content-Type': 'application/json'}
CARELINK_BASE = 'https://carelink.minimed.eu'
TREND_MAP = {
    'NONE':'NONE','FLAT':'Flat','SLIGHTLY_UP':'FortyFiveUp','UP':'SingleUp',
    'RAPIDLY_UP':'DoubleUp','SLIGHTLY_DOWN':'FortyFiveDown','DOWN':'SingleDown',
    'RAPIDLY_DOWN':'DoubleDown'
}

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
    'Accept': 'text/html,application/json,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
})

def extract_hidden_fields(html):
    """Extract all hidden input fields from an HTML form."""
    fields = {}
    for m in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', html, re.IGNORECASE):
        tag = m.group(0)
        name  = re.search(r'name=["\']([^"\']+)["\']', tag)
        value = re.search(r'value=["\']([^"\']*)["\']', tag)
        if name:
            fields[name.group(1)] = value.group(1) if value else ''
    return fields

def login():
    print('Logging in to CareLink EU...')

    # Step 1: land on CareLink SSO — redirects to Auth0
    r1 = session.get(
        f'{CARELINK_BASE}/patient/sso/login',
        params={'country': CARELINK_COUNTRY, 'lang': 'en'},
        allow_redirects=True
    )
    print(f'Login page: {r1.status_code} {r1.url}')

    # Step 2: parse hidden fields from Auth0 form
    hidden = extract_hidden_fields(r1.text)
    print(f'Hidden fields: {list(hidden.keys())}')

    # Build form data — hidden fields + credentials
    form_data = dict(hidden)
    form_data['username'] = CARELINK_USERNAME
    form_data['password'] = CARELINK_PASSWORD
    form_data['action']   = 'default'

    # Step 3: POST to the Auth0 login URL
    r2 = session.post(
        r1.url,
        data=form_data,
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'Referer': r1.url},
        allow_redirects=True
    )
    print(f'Auth POST: {r2.status_code} → {r2.url[:80]}')

    # Check for error in response
    if 'Wrong email or password' in r2.text or 'error' in r2.url.lower():
        print('ERROR: Wrong credentials')
        return False

    print(f'Cookies: {list(session.cookies.keys())}')
    return True

def fetch_data():
    url = f'{CARELINK_BASE}/patient/connect/data'
    params = {'cpSerialNumber':'NONE','msgType':'last24hours',
              'requestTime': int(time.time()*1000)}
    r = session.get(url, params=params)
    print(f'Data fetch: {r.status_code}')
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            print(f'Response (first 300): {r.text[:300]}')
    return None

def upload_glucose(glucose, trend_raw):
    trend = TREND_MAP.get(trend_raw, 'NONE')
    entry = {'type':'sgv','sgv':glucose,
             'date':int(time.time()*1000),
             'dateString':datetime.now(timezone.utc).isoformat(),
             'direction':trend,'device':'Medtronic780G'}
    r = requests.post(f'{NS_HOST}/api/v1/entries', json=[entry], headers=NS_HEADERS)
    print(f'NS: {r.status_code} — {glucose} mg/dL {trend}')

def upload_pump(reservoir, battery):
    status = {'device':'Medtronic780G',
              'created_at':datetime.now(timezone.utc).isoformat(),
              'pump':{'reservoir':reservoir,'battery':{'percent':battery}}}
    requests.post(f'{NS_HOST}/api/v1/devicestatus', json=[status], headers=NS_HEADERS)

def main():
    print('Starting CareLink uploader...')
    if not login():
        print('Login failed — exiting')
        return

    while True:
        try:
            data = fetch_data()
            if data:
                sg = data.get('lastSG', {})
                glucose = sg.get('sg', 0)
                if glucose:
                    upload_glucose(glucose, data.get('lastSGTrend','NONE'))
                    upload_pump(data.get('reservoirRemainingUnits',0),
                                data.get('conduitBatteryLevel',0))
                else:
                    print('No glucose in response')
            else:
                print('No data — re-logging...')
                login()
        except Exception as e:
            print(f'Error: {e}')
            try:
                login()
            except Exception:
                pass
        time.sleep(INTERVAL)

if __name__ == '__main__':
    main()
