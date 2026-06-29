import os
import json
import time
from datetime import datetime, timezone
from carelink_client import CareLinkClient

INTERVAL        = int(os.environ.get('UPLOAD_INTERVAL', '300'))
PATIENT_ID      = os.environ.get('PATIENT_ID', '')  # legacy single-patient fallback
REFRESH_CYCLES  = 12  # re-fetch patient list every N cycles (~1 h at 300 s)

TREND_MAP = {
    'NONE': 'stable', 'FLAT': 'stable',
    'SLIGHTLY_UP': 'rising', 'UP': 'rising', 'RAPIDLY_UP': 'risingFast',
    'SLIGHTLY_DOWN': 'falling', 'DOWN': 'falling', 'RAPIDLY_DOWN': 'fallingFast',
}

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


def fetch_active_patients():
    """Return list of patient IDs where monitoringStatus in ('active', 'needs_reauth')."""
    if not _fs:
        return [PATIENT_ID] if PATIENT_ID else []
    try:
        docs = _fs.collection('patients').where('monitoringStatus', 'in', ['active', 'needs_reauth']).stream()
        ids = [d.id for d in docs]
        print(f'Active patients: {ids}')
        return ids
    except Exception as e:
        print(f'fetch_active_patients error: {e}')
        return [PATIENT_ID] if PATIENT_ID else []


def mark_needs_reauth(patient_id):
    if not _fs:
        return
    try:
        _fs.collection('patients').document(patient_id).set(
            {'monitoringStatus': 'needs_reauth'},
            merge=True,
        )
        print(f'{patient_id}: marked needs_reauth')
    except Exception as e:
        print(f'{patient_id}: mark_needs_reauth error: {e}')


def _save_cookies(patient_id, cookies):
    if not _fs:
        return
    try:
        (_fs.collection('patients').document(patient_id)
            .collection('secrets').document('carelinkSession')
            .set({'cookies': cookies, 'updatedAt': datetime.now(timezone.utc).isoformat()}))
        print(f'{patient_id}: saved {len(cookies)} CareLink cookies')
    except Exception as e:
        print(f'{patient_id}: cookie save error: {e}')


def _load_cookies(patient_id):
    """Load from Firestore; fall back to COOKIE_JAR env (single-patient legacy)."""
    reseed = os.environ.get('RESEED') == '1'
    if not reseed and _fs:
        try:
            doc = (_fs.collection('patients').document(patient_id)
                      .collection('secrets').document('carelinkSession').get())
            if doc.exists:
                cookies = (doc.to_dict() or {}).get('cookies')
                if cookies:
                    print(f'{patient_id}: loaded {len(cookies)} cookies from Firestore')
                    return cookies
        except Exception as e:
            print(f'{patient_id}: cookie load error: {e}')
    # Legacy single-patient env fallback.
    if patient_id == PATIENT_ID:
        env = os.environ.get('COOKIE_JAR')
        if env:
            try:
                cookies = json.loads(env)
                print(f'{patient_id}: loaded {len(cookies)} cookies from COOKIE_JAR env (seed)')
                _save_cookies(patient_id, cookies)
                return cookies
            except Exception as e:
                print(f'{patient_id}: COOKIE_JAR parse error: {e}')
    return None


def _write_glucose_history(patient_id, agg1d):
    if not _fs or not agg1d:
        return
    try:
        col = _fs.collection('patients').document(patient_id).collection('glucoseByDay')
        for day in agg1d:
            readings = [
                {'ts': r['ts'] * 1000, 'sgv': r['sg']}
                for r in day.get('sg', {}).get('sgVal', [])
                if r.get('sg', 0) > 0
            ]
            if not readings:
                continue
            date_str = datetime.fromtimestamp(readings[0]['ts'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
            col.document(date_str).set({'readings': readings, 'updatedAt': datetime.now(timezone.utc).isoformat()})
        print(f'{patient_id}: glucose history written ({len(agg1d)} days)')
    except Exception as e:
        print(f'{patient_id}: history error: {e}')


def _write_rt_history(patient_id, sgs):
    if not _fs or not sgs:
        return
    try:
        col = _fs.collection('patients').document(patient_id).collection('glucoseByDay')
        by_day = {}
        for r in sgs:
            date_str = datetime.fromtimestamp(r['ts'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
            by_day.setdefault(date_str, []).append(r)
        for date_str, readings in by_day.items():
            col.document(date_str).set({'readings': readings, 'updatedAt': datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        print(f'{patient_id}: rt-history error: {e}')


def write_to_firestore(patient_id, rt, batch):
    if not _fs:
        return
    try:
        from firebase_admin import firestore as fb_firestore
        batch_data = batch or {}
        rt    = rt or {}
        s7   = batch_data.get('stats7d',    {})
        s14  = batch_data.get('stats14d',   {})
        s30  = batch_data.get('stats30d',   {})
        stod = batch_data.get('statsToday', {})
        pi   = batch_data.get('pumpInfo',   {})
        pump = rt.get('pump', {})
        now  = datetime.now(timezone.utc).isoformat()
        patient_name = batch_data.get('patientName')

        meta = _fs.collection('patients').document(patient_id).collection('meta')

        vitals = {'patientName': patient_name, 'updatedAt': now}
        if rt.get('glucose'):
            vitals['glucose'] = rt['glucose']
            vitals['trend']   = rt.get('trend', 'stable')
        meta.document('latestVitals').set(vitals, merge=True)

        sensor_state = pump.get('sensorState')
        SENSOR_LIFE_H = 168
        raw_remaining = pump.get('sensorDurationHours')
        sensor_age = (SENSOR_LIFE_H - raw_remaining) if raw_remaining is not None else None
        pump_doc = {k: v for k, v in {
            'pumpModel':        pump.get('pumpModel') or pi.get('pumpModel'),
            'sensorModel':      pi.get('sensorModel'),
            'autoMode':         stod.get('autoMode'),
            'reservoirLevel':   pump.get('reservoirUnits'),
            'reservoirPercent': pump.get('reservoirPercent'),
            'batteryLevel':     pump.get('batteryPercent'),
            'activeInsulin':    pump.get('activeInsulin'),
            'sensorBattery':    pump.get('sensorBattery'),
            'sensorAgeHours':   None if sensor_state == 'WARM_UP' else sensor_age,
            'sensorState':      sensor_state,
            'pumpMode':         'suspended' if pump.get('suspended') else 'auto',
        }.items() if v is not None}
        pump_doc['patientName'] = patient_name
        pump_doc['updatedAt']   = now
        meta.document('latestPump').set(pump_doc, merge=True)
        if sensor_state == 'WARM_UP':
            meta.document('latestPump').update({'sensorAgeHours': fb_firestore.DELETE_FIELD})

        meta.document('latestStats').set({
            'today': {k: v for k, v in stod.items() if v is not None},
            '7d':    {k: v for k, v in s7.items()   if v is not None},
            '14d':   {k: v for k, v in s14.items()  if v is not None},
            '30d':   {k: v for k, v in s30.items()  if v is not None},
            'pumpModel':   pump.get('pumpModel') or pi.get('pumpModel'),
            'sensorModel': pi.get('sensorModel'),
            'updatedAt':   now,
        })

        _write_glucose_history(patient_id, batch_data.get('rawAgg1d', []))
        _write_rt_history(patient_id, rt.get('sgs', []))
        if rt.get('glucose'):
            print(f'{patient_id}: written sg={rt["glucose"]} trend={rt.get("trend", "stable")} '
                  f'TIR7d={s7.get("tirNormal")}%')
        else:
            print(f'{patient_id}: sensor gap — stats/pump updated TIR7d={s7.get("tirNormal")}%')
    except Exception as e:
        print(f'{patient_id}: Firestore write error: {e}')


def run_patient(patient_id, client):
    """Fetch and store one cycle for a single patient. Returns False if auth failed."""
    try:
        batch = client.getRecentData()
        rt    = client.getRealtimeData()
        if (rt and rt.get('glucose')) or (batch and batch.get('glucose')):
            write_to_firestore(patient_id, rt, batch)
        else:
            print(f'{patient_id}: no glucose reading')
        return True
    except Exception as e:
        msg = str(e).lower()
        if 'reauth' in msg or '401' in msg or '403' in msg or 'auth' in msg:
            return False
        print(f'{patient_id}: error: {e}')
        return True


def main():
    print('Starting CareLink multi-tenant uploader...')
    _init_firestore()

    patient_ids = fetch_active_patients()
    if not patient_ids:
        print('No active patients found. Set PATIENT_ID or add patients with monitoringStatus=active.')
        return

    clients = {}
    for pid in patient_ids:
        cookies = _load_cookies(pid)
        if cookies:
            clients[pid] = CareLinkClient(
                cookies=cookies,
                on_cookies_updated=lambda c, p=pid: _save_cookies(p, c),
            )
        else:
            print(f'{pid}: no cookies found — skipping')

    if not clients:
        print('No clients with cookies. Exiting.')
        return

    print(f'Ready — monitoring {len(clients)} patient(s): {list(clients.keys())}')
    cycle = 0

    while True:
        cycle += 1
        for patient_id, client in list(clients.items()):
            ok = run_patient(patient_id, client)
            if not ok:
                mark_needs_reauth(patient_id)

        # Refresh patient list periodically to pick up newly registered patients.
        if cycle % REFRESH_CYCLES == 0:
            new_ids = fetch_active_patients()
            for pid in new_ids:
                if pid not in clients:
                    cookies = _load_cookies(pid)
                    if cookies:
                        clients[pid] = CareLinkClient(
                            cookies=cookies,
                            on_cookies_updated=lambda c, p=pid: _save_cookies(p, c),
                        )
                        print(f'Added new patient: {pid}')

        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
