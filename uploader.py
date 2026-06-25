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


def _write_rt_history(sgs: list):
    """Write live display/message readings (high-res, last 24h) grouped by day."""
    if not _fs or not sgs:
        return
    try:
        col = _fs.collection('patients').document(PATIENT_ID).collection('glucoseByDay')
        by_day: dict = {}
        for r in sgs:
            date_str = datetime.fromtimestamp(r['ts'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
            by_day.setdefault(date_str, []).append(r)
        for date_str, readings in by_day.items():
            col.document(date_str).set(
                {'readings': readings, 'updatedAt': datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        print(f'Firestore rt-history error: {e}')


def write_to_firestore(rt, batch):
    if not _fs:
        return
    try:
        batch = batch or {}
        rt    = rt or {}
        s7   = batch.get('stats7d',    {})
        s14  = batch.get('stats14d',   {})
        s30  = batch.get('stats30d',   {})
        stod = batch.get('statsToday', {})
        pi   = batch.get('pumpInfo',   {})
        pump = rt.get('pump', {})
        now  = datetime.now(timezone.utc).isoformat()
        patient_name = batch.get('patientName')

        meta = _fs.collection('patients').document(PATIENT_ID).collection('meta')

        # Current glucose comes ONLY from real-time. The batch/daily value is a stale
        # summary artifact (a fixed ~113) and must never clobber the live reading. On a
        # sensor gap we omit glucose/trend entirely — merge=True keeps the last good
        # value, and the app's freshness indicator then flags it as not-transmitting.
        vitals = {'patientName': patient_name, 'updatedAt': now}
        if rt.get('glucose'):
            vitals['glucose'] = rt['glucose']
            vitals['trend']   = rt.get('trend', 'stable')
        meta.document('latestVitals').set(vitals, merge=True)

        # Only write fields that are non-None — merge=True with a None value would
        # overwrite a previously good reading with null (e.g. sensorAgeHours during gap).
        sensor_state = pump.get('sensorState')
        pump_doc = {k: v for k, v in {
            'pumpModel':      pump.get('pumpModel') or pi.get('pumpModel'),
            'sensorModel':    pi.get('sensorModel'),
            'autoMode':       stod.get('autoMode'),
            'reservoirLevel':   pump.get('reservoirUnits'),
            'reservoirPercent': pump.get('reservoirPercent'),
            'batteryLevel':     pump.get('batteryPercent'),
            'activeInsulin':    pump.get('activeInsulin'),
            'sensorBattery':    pump.get('sensorBattery'),
            # During WARM_UP the sensorDurationHours is stale from the old sensor — omit it.
            'sensorAgeHours':   None if sensor_state == 'WARM_UP' else pump.get('sensorDurationHours'),
            'sensorState':      sensor_state,
            'pumpMode':         'suspended' if pump.get('suspended') else 'auto',
        }.items() if v is not None}
        pump_doc['patientName'] = patient_name
        pump_doc['updatedAt']   = now
        meta.document('latestPump').set(pump_doc, merge=True)
        # merge=True never deletes fields — explicitly clear stale age during warm-up.
        if sensor_state == 'WARM_UP':
            meta.document('latestPump').update({'sensorAgeHours': firestore.firestore.DELETE_FIELD})

        meta.document('latestStats').set({
            'today': {k: v for k, v in stod.items() if v is not None},
            '7d':    {k: v for k, v in s7.items()   if v is not None},
            '14d':   {k: v for k, v in s14.items()  if v is not None},
            '30d':   {k: v for k, v in s30.items()  if v is not None},
            'pumpModel':   pump.get('pumpModel') or pi.get('pumpModel'),
            'sensorModel': pi.get('sensorModel'),
            'updatedAt':   now,
        })

        # Glucose history: batch fills older days; live sgs overwrite recent days hi-res.
        _write_glucose_history(batch.get('rawAgg1d', []))
        _write_rt_history(rt.get('sgs', []))
        if rt.get('glucose'):
            print(f'Firestore: written — sg={rt["glucose"]} (live) '
                  f'trend={rt.get("trend", "stable")} TIR7d={s7.get("tirNormal")}%')
        else:
            print(f'Firestore: sensor gap — glucose left unchanged; stats/pump updated. '
                  f'TIR7d={s7.get("tirNormal")}%')
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
            batch = client.getRecentData()       # reauths; multi-day stats + patient name
            rt    = client.getRealtimeData()      # live SG + pump status
            if (rt and rt.get('glucose')) or (batch and batch.get('glucose')):
                write_to_firestore(rt, batch)
            else:
                print('No glucose reading')
        except Exception as e:
            print(f'Error: {e}')
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
