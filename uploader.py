import os
import time
import hashlib
import requests
from datetime import datetime, timezone

from CaRelinkClient.CaRelinkClient import CareLinkClient

CARELINK_USERNAME = os.environ['CARELINK_USERNAME']
CARELINK_PASSWORD = os.environ['CARELINK_PASSWORD']
CARELINK_COUNTRY = os.environ.get('CARELINK_COUNTRY', 'il')
NS_HOST = os.environ['NS_HOST'].rstrip('/')
API_SECRET = os.environ['API_SECRET']
INTERVAL = int(os.environ.get('UPLOAD_INTERVAL', '300'))

API_SECRET_HASH = hashlib.sha1(API_SECRET.encode()).hexdigest()
NS_HEADERS = {'API-SECRET': API_SECRET_HASH, 'Content-Type': 'application/json'}

TREND_MAP = {
    'NONE': 'NONE', 'FLAT': 'Flat',
    'SLIGHTLY_UP': 'FortyFiveUp', 'UP': 'SingleUp', 'RAPIDLY_UP': 'DoubleUp',
    'SLIGHTLY_DOWN': 'FortyFiveDown', 'DOWN': 'SingleDown', 'RAPIDLY_DOWN': 'DoubleDown'
}

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
    print(f'Glucose {glucose} mg/dL ({trend}) → NS: {r.status_code}')

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
    client = CareLinkClient(CARELINK_USERNAME, CARELINK_PASSWORD, CARELINK_COUNTRY)

    if not client.login():
        print('Login failed — check credentials')
        return

    print('Login successful')

    while True:
        try:
            data = client.getRecentData()
            if data:
                sg = data.get('lastSG', {})
                glucose = sg.get('sg', 0)
                if glucose:
                    trend = data.get('lastSGTrend', 'NONE')
                    upload_glucose(glucose, trend)
                    upload_pump(
                        data.get('reservoirRemainingUnits', 0),
                        data.get('conduitBatteryLevel', 0)
                    )
                else:
                    print('No glucose reading available')
            else:
                print('No data returned')
        except Exception as e:
            print(f'Error: {e}')

        time.sleep(INTERVAL)

if __name__ == '__main__':
    main()
