import os
import json
import time
import hashlib
import requests
from datetime import datetime, timezone
from carelink_client import CareLinkClient

NS_HOST    = os.environ['NS_HOST'].rstrip('/')
API_SECRET = os.environ['API_SECRET']
INTERVAL   = int(os.environ.get('UPLOAD_INTERVAL', '300'))
PATIENT_ID = os.environ.get('PATIENT_ID', 'patient_001')

API_SECRET_HASH = hashlib.sha1(API_SECRET.encode()).hexdigest()
NS_HEADERS = {'API-SECRET': API_SECRET_HASH, 'Content-Type': 'application/json'}

TREND_MAP = {
    'NONE': 'NONE', 'FLAT': 'Flat', 'SLIGHTLY_UP': 'FortyFiveUp',
    'UP': 'SingleUp', 'RAPIDLY_UP': 'DoubleUp',
    'SLIGHTLY_DOWN': 'FortyFiveDown', 'DOWN': 'SingleDown',
    'RAPIDLY_DOWN': 'DoubleDown',
}

# Nightscout → Firestore trend values (match VoiceCare GlucoseTrend type)
NS_TO_TREND = {
    'DoubleUp': 'risingFast', 'SingleUp': 'rising', 'FortyFiveUp': 'rising',
    'Flat': 'stable', 'NONE': 'stable',
    'FortyFiveDown': 'falling', 'SingleDown': 'falling', 'DoubleDown': 'fallingFast',
}

# --- Firestore setup (optional — skipped if FIREBASE_SERVICE_ACCOUNT not set) ---
_fs = None

def _init_firestore():
    global _fs
    sa_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
    if not sa_json:
        print('Firestore: FIREBASE_SERVICE_ACCOUNT not set — skipping Firestore writes')
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        if not firebase_admin._apps:
            sa = json.loads(sa_json)
            cred = credentials.Certificate(sa)
            firebase_admin.initialize_app(cred)
        _fs = firestore.client()
        print('Firestore: connected')
    except Exception as e:
        print(f'Firestore init error: {e}')


def _write_glucose_history(agg1d: list):
    """Write sgVal readings to patients/{id}/glucoseByDay/{YYYY-MM-DD}."""
    if not _fs or not agg1d:
        return
    try:
        col = _fs.collection('patients').document(PATIENT_ID).collection('glucoseByDay')
        for day in agg1d:
            readings = [
                {'ts': r['ts'] * 1000, 'sgv': r['sg']}
                for r in day.get('sg', {}).get('sgVal', [])
                if r.get('sg', 0) > 0
            ]
            if not readings:
                continue
            # Use the date of the first reading as doc ID
            date_str = datetime.fromtimestamp(
                readings[0]['ts'] / 1000, tz=timezone.utc
            ).strftime('%Y-%m-%d')
            col.document(date_str).set({'readings': readings, 'updatedAt': datetime.now(timezone.utc).isoformat()})
        print(f'Firestore: glucose history written ({len(agg1d)} days)')
    except Exception as e:
        print(f'Firestore history error: {e}')


def write_to_firestore(data):
    if not _fs:
        return
    try:
        s7   = data.get('stats7d',    {})
        s14  = data.get('stats14d',   {})
        s30  = data.get('stats30d',   {})
        stod = data.get('statsToday', {})
        pi   = data.get('pumpInfo',   {})
        ns_trend = TREND_MAP.get(data.get('trend', 'NONE'), 'NONE')
        trend    = NS_TO_TREND.get(ns_trend, 'stable')
        now = datetime.now(timezone.utc).isoformat()

        meta = _fs.collection('patients').document(PATIENT_ID).collection('meta')

        meta.document('latestVitals').set({
            'glucose':   data['glucose'],
            'trend':     trend,
            'updatedAt': now,
        }, merge=True)

        meta.document('latestPump').set({
            'pumpModel':   pi.get('pumpModel'),
            'sensorModel': pi.get('sensorModel'),
            'autoMode':    stod.get('autoMode'),
            'updatedAt':   now,
        }, merge=True)

        meta.document('latestStats').set({
            'today': {k: v for k, v in stod.items() if v is not None},
            '7d':    {k: v for k, v in s7.items()   if v is not None},
            '14d':   {k: v for k, v in s14.items()  if v is not None},
            '30d':   {k: v for k, v in s30.items()  if v is not None},
            'pumpModel':   pi.get('pumpModel'),
            'sensorModel': pi.get('sensorModel'),
            'updatedAt':   now,
        })

        # Write individual readings grouped by day
        _write_glucose_history(data.get('rawAgg1d', []))
        print(f'Firestore: written — sg={data["glucose"]} TIR7d={s7.get("tirNormal")}%')
    except Exception as e:
        print(f'Firestore write error: {e}')


# --- Nightscout ---

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
    s7   = data.get('stats7d',  {})
    s14  = data.get('stats14d', {})
    s30  = data.get('stats30d', {})
    stod = data.get('statsToday', {})
    pi   = data.get('pumpInfo', {})

    status = {
        'device': 'Medtronic780G',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'pump': {
            'autoMode':    stod.get('autoMode'),
            'pumpModel':   pi.get('pumpModel'),
            'sensorModel': pi.get('sensorModel'),
        },
        'cgmStats': {
            'today': stod,
            '7d':    s7,
            '14d':   s14,
            '30d':   s30,
        },
    }
    r = requests.post(f'{NS_HOST}/api/v1/devicestatus', json=[status], headers=NS_HEADERS)
    print(f'NS devicestatus: {r.status_code} — TIR7d={s7.get("tirNormal")}% avg7d={s7.get("avgSG")} TDD7d={s7.get("tdd")}u')


def main():
    print('Starting CareLink uploader...')
    _init_firestore()
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
                write_to_firestore(data)
            else:
                print('No glucose reading')
        except Exception as e:
            print(f'Error: {e}')
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
