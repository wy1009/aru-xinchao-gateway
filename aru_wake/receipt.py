"""Answer one scoped receipt query from the local adapter over a systemd socket."""
import hmac
import json
import sys
from pathlib import Path

def lookup(state, query):
    ep = next((e for e in state.get("wakeBridgeEndpoints", [])
               if e["endpointId"] == query["endpoint"] and not e.get("disabledAt")
               and hmac.compare_digest(e["submitTokenHash"], query["tokenHash"])), None)
    if not ep:
        return "unregistered"
    event = next((e for e in state.get("wakeBridgeEvents", [])
                  if e["eventId"] == query["event"] and e["endpointId"] == ep["endpointId"]), None)
    if event is None:
        return "missing"
    if event.get("acknowledgedAt"):
        return "phone_received"
    return "retry_relay" if ep.get("lastFailure") else "at_host"

if __name__ == "__main__":
    try:
        query = json.loads(sys.stdin.buffer.readline(1024))
        state = json.loads(Path("/var/lib/aru-selfhost/data/state.json").read_bytes())
        print(lookup(state, query), flush=True)
    except (OSError, KeyError, TypeError, ValueError):
        print("unavailable", flush=True)
