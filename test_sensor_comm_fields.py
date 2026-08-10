"""
Local manual test: verifies _parse_realtime forwards the new sensor-communication fields
(conduitSensorInRange, pumpCommunicationState, gstCommunicationState, tsMs) from a REAL captured
CareLink response, so the sensor-signal-loss fix (2026-07-29) can be checked without hitting the
live API. Runs against raw_display.json — a normal/healthy-state sample, so this only proves the
fields are read and forwarded correctly; it can't confirm what they look like during an actual
fault (no such sample has been captured yet).

Usage (from the carelink-uploader folder):
  .\.venv\Scripts\python.exe test_sensor_comm_fields.py
"""
import json
import os
import sys

from carelink_client import CareLinkClient


def main():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw_display.json")
    if not os.path.exists(path):
        print(f"SKIP: {path} not found")
        return 0

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    client = CareLinkClient()
    result = client._parse_realtime(raw)

    if result is None:
        print("FAIL: _parse_realtime returned None on a sample that has a glucose reading")
        return 1

    pump = result.get("pump", {})
    checks = [
        ("tsMs (real sensor reading timestamp)", result.get("tsMs")),
        ("pump.conduitSensorInRange", pump.get("conduitSensorInRange")),
        ("pump.pumpCommunicationState", pump.get("pumpCommunicationState")),
        ("pump.gstCommunicationState", pump.get("gstCommunicationState")),
    ]
    ok = True
    for label, value in checks:
        status = "OK" if value is not None else "MISSING"
        if value is None:
            ok = False
        print(f"{status:8s} {label} = {value!r}")

    if ok:
        print("\nPASS: all new fields present and forwarded from the raw sample.")
        return 0
    print("\nFAIL: one or more new fields were not forwarded — check the raw JSON key names"
          " against carelink_client.py's _parse_realtime.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
