import os
import time
import requests
import json
import hashlib
from datetime import datetime, timezone

CARELINK_USERNAME = os.environ['CARELINK_USERNAME']
CARELINK_PASSWORD = os.environ['CARELINK_PASSWORD']
CARELINK_COUNTRY = os.environ.get('CARELINK_COUNTRY', 'il')
NS_HOST = os.environ['NS_HOST'].rstrip('/')
API_SECRET = os.environ['API_SECRET']
INTERVAL = int(os.environ.get('UPLOAD_INTERVAL', '300'))

API_SECRET_HASH = hashlib.sha1(API_SECRET.encode()).hexdigest()

CARELINK_BASE = 'https://carelink.minimed.eu'
if CARELINK_COUNTRY == 'us':
    CARELINK_BASE = 'https://carelink.minimed.com'

session = requests.Session()
session.headers.update({'Accept': 'application/json', 'Content-Type': 'application/json'})

def login():
    print('Logging in to CareLink...')
    login_url = f'{CARELINK_BASE}/patient/sso/login?country={CARELINK_COUNTRY}&lang=en'
    r = session.get(login_url)

    auth_url = f'{CARELINK_BASE}/patient/sso/login'
    payload = {
        'username': CARELINK_USERNAME,
        'password': CARELINK_PASSWORD,
    }
    r = session.post(auth_url, json=payload)
    if r.status_code not in (200, 302):
        print(f'Login failed: {r.status_code}')
        return False
    print('Login successful')
    return True

def fetch_data():
    url = f'{CARELINK_BASE}/patient/connect/data?cpSerialNumber=NONE&msgType=last24hours&requestTime={int(time.time()*1000)}'
    r = session.get(url)
    if r.status_code == 401:
        print('Session expired, re-logging...')
        login()
        r = session.get(url)
    if r.status_code != 200:
        print(f'Fetch failed: {r.status_code}')
        return None
    return r.json()

def upload_to_nightscout(data):
    if not data:
        return

    sg = data.get('lastSG', {})
    glucose = sg.get('sg', 0)
    if not glucose:
        print('No glucose value')
        return

    timestamp = sg.get('datetime', datetime.now(timezone.utc).isoformat())
    trend_map = {
        'NONE': 'NONE', 'FLAT': 'Flat', 'SLIGHTLY_UP': 'FortyFiveUp',
        'UP': 'SingleUp', 'RAPIDLY_UP': 'DoubleUp',
        'SLIGHTLY_DOWN': 'FortyFiveDown', 'DOWN': 'SingleDown', 'RAPIDLY_DOWN': 'DoubleDown'
    }
    trend_raw = data.get('lastSGTrend', 'NONE')
    trend = trend_map.get(trend_raw, 'NONE')

    entry = {
        'type': 'sgv',
        'sgv': glucose,
        'date': int(time.time() * 1000),
        'dateString': timestamp,
        'direction': trend,
        'device': 'Medtronic780G'
    }

    headers = {'API-SECRET': API_SECRET_HASH, 'Content-Type': 'application/json'}
    r = requests.post(f'{NS_HOST}/api/v1/entries', json=[entry], headers=headers)
    if r.status_code in (200, 201):
        print(f'Uploaded glucose: {glucose} mg/dL, trend: {trend}')
    else:
        print(f'Upload failed: {r.status_code} {r.text}')

    # Upload pump status
    pump_status = {
        'device': 'Medtronic780G',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'pump': {
            'reservoir': data.get('reservoirRemainingUnits', 0),
            'battery': {'percent': data.get('conduitBatteryLevel', 0)},
            'clock': timestamp,
        },
        'uploaderBattery': data.get('conduitBatteryLevel', 0)
    }
    requests.post(f'{NS_HOST}/api/v1/devicestatus', json=[pump_status], headers=headers)

def main():
    if not login():
        print('Could not log in. Check credentials.')
        return
    while True:
        try:
            data = fetch_data()
            upload_to_nightscout(data)
        except Exception as e:
            print(f'Error: {e}')
        time.sleep(INTERVAL)

if __name__ == '__main__':
    main()
