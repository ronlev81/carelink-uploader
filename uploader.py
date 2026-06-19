import os
import time
import hashlib
import requests
from datetime import datetime, timezone
from urllib.parse import urlencode

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
session.headers.update({
    'User-Agent': 'Mozilla/5.0',
    'Accept': 'application/json, text/plain, */*',
})

def login():
    print('Logging in to CareLink EU...')
    # Step 1: get login page to obtain cookies
    r = session.get(
        f'{CARELINK_BASE}/patient/sso/login',
        params={'country': CARELINK_COUNTRY, 'lang': 'en'},
        allow_redirects=True
    )
    print(f'Login page: {r.status_code} {r.url}')

    # Step 2: post credentials to the SSO form
    login_data = {
        'username': CARELINK_USERNAME,
        'password': CARELINK_PASSWORD,
    }
    r2 = session.post(r.url, data=login_data, allow_redirects=True)
    print(f'Auth response: {r2.status_code}')

    # Step 3: get auth token
    r3 = session.get(
        f'{CARELINK_BASE}/patient/sso/login',
        params={'country': CARELINK_COUNTRY, 'lang': 'en'},
        allow_redirects=True
    )
    print(f'Token response: {r3.status_code}')

    # Check if we have an auth cookie
    cookies = dict(session.cookies)
    print(f'Cookies: {list(cookies.keys())}')
    return r2.status_code in (200, 302) or len(cookies) > 0

def fetch_data():
    url = f'{CARELINK_BASE}/patient/connect/data'
    params = {
        'cpSerialNumber': 'NONE',
        'msgType': 'last24hours',
        'requestTime': int(time.time() * 1000)
    }
    r = session.get(url, params=params)
    print(f'Data fetch: {r.status_code}')
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            print(f'Non-JSON response: {r.text[:200]}')
    return None

def upload_glucose(glucose, trend_raw):
    trend = TREND_MAP.get(trend_raw, 'NONE')
    entry = {
        'type': 'sgv',
        'sgv': glucose,
        'date': int(time.time() * 1000),
        'dateString': datetime.now(timezone.utc).isoformat(),
        'direction': trend,
        'device': 'Medtronic780G'
    }
    r = requests.post(f'{NS_HOST}/api/v1/entries', json=[entry], headers=NS_HEADERS)
    print(f'NS glucose upload: {r.status_code} — {glucose} mg/dL {trend}')

def upload_pump(reservoir, battery):
    status = {
        'device': 'Medtronic780G',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'pump': {
            'reservoir': reservoir,
            'battery': {'percent': battery},
        }
    }
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
                    upload_pump(
                        data.get('reservoirRemainingUnits', 0),
                        data.get('conduitBatteryLevel', 0)
                    )
                    print(f'OK: {glucose} mg/dL')
                else:
                    print('No glucose value in response')
            else:
                print('No data — re-login...')
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
