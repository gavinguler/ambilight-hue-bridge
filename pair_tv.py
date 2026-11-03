"""Pair with a Philips Android TV over JointSpace v6 (digest auth pairing).

Step 1:  python3 pair_tv.py request <tv-ip>   -> the TV shows a PIN
Step 2:  python3 pair_tv.py grant <PIN>       -> credentials saved to config.json
"""
import base64
import hashlib
import hmac
import json
import os
import random
import ssl
import string
import sys
import time
import urllib.request
from pathlib import Path

CONFIG = Path(os.environ.get("DATA_DIR", Path(__file__).parent)) / "config.json"
# Shared secret used by the official Philips TV Remote app for pairing signatures.
SECRET = base64.b64decode(
    "ZmVay1EQVFOaZhwQ4Kv81ypLAZNczV9sG4KkseXWn1NEk6cXmPKO/MCa9sryslvLCFMnNe4Z4CPXzToowvhHvA=="
)
SSL_CTX = ssl._create_unverified_context()  # TV uses a self-signed certificate


def load_config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def save_config(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))
    CONFIG.chmod(0o600)


def base(ip):
    return f"https://{ip}:1926/6"


def device_info(device_id):
    return {
        "device_name": "ambilight-hue-bridge",
        "device_os": "Linux",
        "app_name": "AmbilightHueBridge",
        "type": "native",
        "app_id": "app.id",
        "id": device_id,
    }


def post(ip, path, body, user=None, password=None):
    handlers = [urllib.request.HTTPSHandler(context=SSL_CTX)]
    if user:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, base(ip), user, password)
        handlers.append(urllib.request.HTTPDigestAuthHandler(mgr))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        f"{base(ip)}/{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with opener.open(req, timeout=10) as resp:
        text = resp.read().decode()
        return json.loads(text) if text.strip() else {}


def request(ip):
    device_id = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    res = post(ip, "pair/request", {
        "scope": ["read", "write", "control"],
        "device": device_info(device_id),
    })
    print(json.dumps(res, indent=2))
    if res.get("error_id") != "SUCCESS":
        sys.exit("Pair request failed")
    cfg = load_config()
    cfg["tv"] = {
        "ip": ip,
        "device_id": device_id,
        "auth_key": res["auth_key"],
        "timestamp": res["timestamp"],
    }
    save_config(cfg)
    print(f"PIN is now on the TV. Run: python3 {Path(__file__).name} grant <PIN>  (within {res.get('timeout', 60)}s)")


def grant(pin):
    cfg = load_config()
    tv = cfg["tv"]
    sig = hmac.new(SECRET, f"{tv['timestamp']}{pin}".encode(), hashlib.sha1).hexdigest()
    body = {
        "auth": {
            "auth_AppId": "1",
            "pin": str(pin),
            "auth_timestamp": tv["timestamp"],
            "auth_signature": base64.b64encode(sig.encode()).decode(),
        },
        "device": device_info(tv["device_id"]),
    }
    res = post(tv["ip"], "pair/grant", body, tv["device_id"], tv["auth_key"])
    print(json.dumps(res, indent=2))
    if res.get("error_id") not in (None, "SUCCESS"):
        sys.exit("Grant failed")
    tv["paired"] = True
    tv["paired_at"] = int(time.time())
    save_config(cfg)
    print("Paired. Credentials stored in config.json")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "request":
        request(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "grant":
        grant(sys.argv[2])
    else:
        sys.exit(__doc__)
