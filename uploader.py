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


def upload_devicestatus(data):
    """Upload pump status + TIR/avg/TDD stats to Nightscout devicestatus."""
    s7   = data.get('stats7d',  {})
    s14  = data.get('stats14d', {})
    s30  = data.get('stats30d', {})
    stod = data.get('statsToday', {})

    pump_info = data.get('pumpInfo', {})
    status = {
        'device': 'Medtronic780G',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'pump': {
            'autoMode':      stod.get('autoMode'),
            'modelNumber':   pump_info.get('modelNumber'),
            'serialNumber':  pump_info.get('serialNumber'),
            'deviceFamily':  pump_info.get('deviceFamily'),
            'softwareVersion': pump_info.get('softwareVersion'),
            'reservoir':     pump_info.get('reservoir'),
            'battery':       {'percent': pump_info.get('batteryPercent')} if pump_info.get('batteryPercent') is not None else None,
        },
        'cgmStats': {
            'today': {
                'tirNormal':  stod.get('tirNormal'),
                'tirHigh':    stod.get('tirHigh'),
                'tirExtHigh': stod.get('tirExtHigh'),
                'tirLow':     stod.get('tirLow'),
                'avgSG':      stod.get('avgSG'),
            },
            '7d': {
                'tirNormal':  s7.get('tirNormal'),
                'tirHigh':    s7.get('tirHigh'),
                'tirExtHigh': s7.get('tirExtHigh'),
                'tirLow':     s7.get('tirLow'),
                'avgSG':      s7.get('avgSG'),
                'tdd':        s7.get('tdd'),
                'autoMode':   s7.get('autoMode'),
            },
            '14d': {
                'tirNormal':  s14.get('tirNormal'),
                'tirHigh':    s14.get('tirHigh'),
                'tirExtHigh': s14.get('tirExtHigh'),
                'avgSG':      s14.get('avgSG'),
                'tdd':        s14.get('tdd'),
            },
            '30d': {
                'tirNormal':  s30.get('tirNormal'),
                'tirHigh':    s30.get('tirHigh'),
                'tirExtHigh': s30.get('tirExtHigh'),
                'avgSG':      s30.get('avgSG'),
                'tdd':        s30.get('tdd'),
            },
        },
    }

    r = requests.post(f'{NS_HOST}/api/v1/devicestatus', json=[status], headers=NS_HEADERS)
    print(f'NS devicestatus: {r.status_code} — TIR7d={s7.get("tirNormal")}% avg7d={s7.get("avgSG")} TDD7d={s7.get("tdd")}u')


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
            if data and data.get('glucose'):
                upload_glucose(data['glucose'], data.get('trend', 'NONE'))
                upload_devicestatus(data)
            else:
                print('No glucose reading')
        except Exception as e:
            print(f'Error: {e}')
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
