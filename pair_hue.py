"""Create an API user + Entertainment client key on a Philips Hue Bridge.

    python3 pair_hue.py <bridge-ip>      then press the round link button on the bridge

Find the bridge IP in the Hue app (Settings -> Bridges) or at https://discovery.meethue.com.
"""
import json
import socket
import ssl
import sys
import time
import urllib.request

from pair_tv import load_config, save_config

CTX = ssl._create_unverified_context()  # the bridge uses a self-signed certificate


def main(ip):
    body = json.dumps({"devicetype": f"ambilight-hue-bridge#{socket.gethostname()[:19]}",
                       "generateclientkey": True}).encode()
    print("Press the link button on the Hue Bridge (waiting up to 60 s) ...", flush=True)
    deadline = time.time() + 60
    while time.time() < deadline:
        req = urllib.request.Request(f"https://{ip}/api", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        res = json.loads(urllib.request.urlopen(req, context=CTX, timeout=5).read())[0]
        if "success" in res:
            cfg = load_config()
            cfg["hue"] = {"ip": ip, "username": res["success"]["username"],
                          "clientkey": res["success"]["clientkey"]}
            save_config(cfg)
            print("Paired with the Hue Bridge; credentials stored in config.json")
            return
        if res.get("error", {}).get("type") != 101:  # 101 = link button not pressed yet
            sys.exit(f"Hue Bridge error: {res['error'].get('description')}")
        time.sleep(2)
    sys.exit("Link button was not pressed within 60 s")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
