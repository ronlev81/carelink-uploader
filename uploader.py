import os
import time
import hashlib
import requests
from datetime import datetime, timezone
from carelink_client import CareLinkClient

NS_HOST    = os.environ['NS_HOST'].rstrip('/')
API_SECRET = os.environ['API_SECRET']
INTERVAL   = int(os.environ.get('UPLOAD_INTERVAL', '300'))

API_SECRET_HASH = hashlib.sha1(API_SECRET.encode()).hexdigest()
NS_HEADERS = {'API-SECRET': API_SECRET_HASH, 'Content-Type': 'application/json'}

TREND_MAP = {
    'NONE': 'NONE', 'FLAT': 'Flat', 'SLIGHTLY_UP': 'FortyFiveUp',
    'UP': 'SingleUp', 'RAPIDLY_UP': 'DoubleUp',
    'SLIGHTLY_DOWN': 'FortyFiveDown', 'DOWN': 'SingleDown',
    'RAPIDLY_DOWN': 'DoubleDown',
}

def upload_glucose(glucose, trend_raw):
    trend = TREND_MAP.get(trend_raw, 'NONE')
    entry = {
        'type': 'sgv', 'sgv': glucose,
        'date': int(time.time() * 1000),
        'dateString': datetime.now(timezone.utc).isoformat(),
        'direction': trend, 'device': 'Medtronic780G',
    }
    r = requests.post(f'{NS_HOST}/api/v1/entries', json=[entry], headers=NS_HEADERS)
    print(f'NS glucose: {r.status_code} — {glucose} mg/dL {trend}')

def upload_pump(reservoir, battery):
    status = {
        'device': 'Medtronic780G',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'pump': {'reservoir': reservoir, 'battery': {'percent': battery}},
    }
    requests.post(f'{NS_HOST}/api/v1/devicestatus', json=[status], headers=NS_HEADERS)

def extract_data(data):
    """Extract glucose from clcloud personalWebView response.
    Returns (glucose, trend, reservoir, battery)
    personalWebView returns the latest reading: {"sg": <int>, "ts": <unix>}
    """
    if not data:
        return None, None, None, None

    glucose   = data.get('sg')
    trend     = data.get('trend') or 'NONE'
    reservoir = data.get('reservoirRemainingUnits') or 0
    battery   = data.get('conduitBatteryLevel') or 0

    return glucose, trend, reservoir, battery

def main():
    print('Starting CareLink uploader...')
    client = CareLinkClient()
    if not client.login():
        print('Login failed')
        return
    print('Ready')

    while True:
        try:
            data = client.getRecentData()
            glucose, trend, reservoir, battery = extract_data(data)
            if glucose:
                upload_glucose(glucose, trend)
                upload_pump(reservoir, battery)
            else:
                print('No glucose reading')
        except Exception as e:
            print(f'Error: {e}')
        time.sleep(INTERVAL)

if __name__ == '__main__':
    main()
