import os
import time
import hashlib
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

CARELINK_USERNAME = os.environ['CARELINK_USERNAME']
CARELINK_PASSWORD = os.environ['CARELINK_PASSWORD']
CARELINK_COUNTRY = os.environ.get('CARELINK_COUNTRY', 'il')
NS_HOST = os.environ['NS_HOST'].rstrip('/')
API_SECRET = os.environ['API_SECRET']
INTERVAL = int(os.environ.get('UPLOAD_INTERVAL', '300'))

API_SECRET_HASH = hashlib.sha1(API_SECRET.encode()).hexdigest()
NS_HEADERS = {'API-SECRET': API_SECRET_HASH, 'Content-Type': 'application/json'}

CARELINK_BASE = 'https://carelink.minimed.eu'
TREND_MAP = {
    'NONE': 'NONE', 'FLAT': 'Flat',
    'SLIGHTLY_UP': 'FortyFiveUp', 'UP': 'SingleUp', 'RAPIDLY_UP': 'DoubleUp',
    'SLIGHTLY_DOWN': 'FortyFiveDown', 'DOWN': 'SingleDown', 'RAPIDLY_DOWN': 'DoubleDown'
}

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)'})

def login():
    print('Logging in to CareLink EU...')

    # Step 1: get Auth0 login page
    r1 = session.get(
        f'{CARELINK_BASE}/patient/sso/login',
        params={'country': CARELINK_COUNTRY, 'lang': 'en'},
        allow_redirects=True
    )
    print(f'Login page: {r1.status_code} {r1.url}')

    # Extract state from Auth0 URL
    parsed = urlparse(r1.url)
    qs = parse_qs(parsed.query)
    state = qs.get('state', [None])[0]
    print(f'Auth0 state: {state[:20] if state else "None"}...')

    # Step 2: POST credentials to Auth0
    login_url = r1.url  # Auth0 login URL
    form_data = {
        'username': CARELINK_USERNAME,
        'password': CARELINK_PASSWORD,
        'action': 'default',
    }
    if state:
        form_data['state'] = state

    r2 = session.post(
        login_url,
        data=form_data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        allow_redirects=True
    )
    print(f'Auth POST: {r2.status_code} → {r2.url}')

    # Step 3: Follow redirect back to CareLink to get session cookie
    if 'carelink' in r2.url or r2.status_code == 200:
        r3 = session.get(f'{CARELINK_BASE}/patient/sso/login',
                         params={'country': CARELINK_COUNTRY, 'lang': 'en'},
                         allow_redirects=True)
        print(f'Session: {r3.status_code}')

    cookies = dict(session.cookies)
    print(f'Cookies: {list(cookies.keys())}')
    return True

def fetch_data():
    url = f'{CARELINK_BASE}/patient/connect/data'
    params = {'cpSerialNumber': 'NONE', 'msgType': 'last24hours',
              'requestTime': int(time.time() * 1000)}
    r = session.get(url, params=params)
    print(f'Data fetch: {r.status_code}')
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            print(f'Response: {r.text[:300]}')
    elif r.status_code == 401:
        print('401 — session expired')
    return None

def upload_glucose(glucose, trend_raw):
    trend = TREND_MAP.get(trend_raw, 'NONE')
    entry = {'type': 'sgv', 'sgv': glucose,
             'date': int(time.time() * 1000),
             'dateString': datetime.now(timezone.utc).isoformat(),
             'direction': trend, 'device': 'Medtronic780G'}
    r = requests.post(f'{NS_HOST}/api/v1/entries', json=[entry], headers=NS_HEADERS)
    print(f'NS: {r.status_code} — {glucose} mg/dL {trend}')

def upload_pump(reservoir, battery):
    status = {'device': 'Medtronic780G',
               'created_at': datetime.now(timezone.utc).isoformat(),
               'pump': {'reservoir': reservoir, 'battery': {'percent': battery}}}
    requests.post(f'{NS_HOST}/api/v1/devicestatus', json=[status], headers=NS_HEADERS)

def main():
    print('Starting CareLink uploader...')
    login()
    while True:
        try:
            data = fetch_data()
            if data:
                sg = data.get('lastSG', {})
                glucose = sg.get('sg', 0)
                if glucose:
                    upload_glucose(glucose, data.get('lastSGTrend', 'NONE'))
                    upload_pump(data.get('reservoirRemainingUnits', 0),
                                data.get('conduitBatteryLevel', 0))
                else:
                    print('No glucose in response')
            else:
                print('Re-logging...')
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
