import os
import json
import time
from datetime import datetime, timezone
from carelink_client import CareLinkClient

INTERVAL   = int(os.environ.get('UPLOAD_INTERVAL', '300'))
PATIENT_ID = os.environ.get('PATIENT_ID', 'patient_001')

# CareLink trend code -> VoiceCare GlucoseTrend value (used for Firestore)
TREND_MAP = {
    'NONE': 'stable', 'FLAT': 'stable',
    'SLIGHTLY_UP': 'rising', 'UP': 'rising', 'RAPIDLY_UP': 'risingFast',
    'SLIGHTLY_DOWN': 'falling', 'DOWN': 'falling', 'RAPIDLY_DOWN': 'fallingFast',
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


# --- CareLink cookie jar persistence (Firestore is the source of truth) ---

def _save_cookies(cookies):
    """Persist the (rolling) CareLink cookie jar after each reauth."""
    if not _fs:
        return
    try:
        (_fs.collection('patients').document(PATIENT_ID)
            .collection('secrets').document('carelinkSession')
            .set({'cookies': cookies, 'updatedAt': datetime.now(timezone.utc).isoformat()}))
        print(f'Firestore: saved {len(cookies)} CareLink cookies')
    except Exception as e:
        print(f'Firestore cookie save error: {e}')


def _load_cookies():
    """Load cookies from Firestore (latest refreshed jar); fall back to COOKIE_JAR env for first seed.

    Set RESEED=1 to force-use COOKIE_JAR env (overwrites the stored jar) — used when the
    Auth0 session finally expires and you've captured a fresh login.
    """
    reseed = os.environ.get('RESEED') == '1'
    if not reseed and _fs:
        try:
            doc = (_fs.collection('patients').document(PATIENT_ID)
                      .collection('secrets').document('carelinkSession').get())
            if doc.exists:
                cookies = (doc.to_dict() or {}).get('cookies')
                if cookies:
                    print(f'Loaded {len(cookies)} CareLink cookies from Firestore')
                    return cookies
        except Exception as e:
            print(f'Firestore cookie load error: {e}')

    env = os.environ.get('COOKIE_JAR')
    if env:
        try:
            cookies = json.loads(env)
            print(f'Loaded {len(cookies)} CareLink cookies from COOKIE_JAR env (initial seed)')
            _save_cookies(cookies)  # promote into Firestore for next boot
            return cookies
        except Exception as e:
            print(f'COOKIE_JAR parse error: {e}')
    return None


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
        trend = TREND_MAP.get(data.get('trend', 'NONE'), 'stable')
        now = datetime.now(timezone.utc).isoformat()

        meta = _fs.collection('patients').document(PATIENT_ID).collection('meta')

        patient_name = data.get('patientName')

        meta.document('latestVitals').set({
            'glucose':     data['glucose'],
            'trend':       trend,
            'patientName': patient_name,
            'updatedAt':   now,
        }, merge=True)

        meta.document('latestPump').set({
            'pumpModel':   pi.get('pumpModel'),
            'sensorModel': pi.get('sensorModel'),
            'autoMode':    stod.get('autoMode'),
            'patientName': patient_name,
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


def main():
    print('Starting CareLink uploader...')
    _init_firestore()

    cookies = _load_cookies()
    if not cookies:
        print('No CareLink cookies found. Seed them once: run the local login + push '
              'to Firestore (patients/<id>/secrets/carelinkSession) or set COOKIE_JAR env.')
        return

    client = CareLinkClient(cookies=cookies, on_cookies_updated=_save_cookies)
    print('Ready (web reauth mode — no browser needed)')

    while True:
        try:
            data = client.getRecentData()
            if data and data.get('glucose'):
                write_to_firestore(data)
            else:
                print('No glucose reading')
        except Exception as e:
            print(f'Error: {e}')
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
