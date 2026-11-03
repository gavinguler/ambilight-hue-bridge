"""Philips Ambilight -> Philips Hue sync over the Hue Entertainment API.

Reads the live colours the TV measures for Ambilight (JointSpace v6) and streams them to
a Hue entertainment area. Waits while the TV is off or Ambilight is dark, and
releases the lights again after a period of darkness.

    python3 sync.py            run the sync only (Ctrl+C to stop)
    python3 sync.py --test     flash each channel to check light positions
"""
import argparse
import json
import math
import os
import shutil
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from tvclient import TVClient

# Data dir holds config.json and bridge state; defaults to the script folder, /data in Docker.
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
CONFIG = DATA_DIR / "config.json"
# The Hue stream needs DTLS 1.2 with PSK; macOS' bundled LibreSSL lacks it, so prefer Homebrew's OpenSSL.
_BREW_OPENSSL = next((p for v in ("openssl@4", "openssl@3") for p in (f"/opt/homebrew/opt/{v}/bin/openssl", f"/usr/local/opt/{v}/bin/openssl")
                      if Path(p).exists()), None)
OPENSSL = os.environ.get("OPENSSL") or _BREW_OPENSSL or shutil.which("openssl") or "openssl"
DEFAULT_AREA = "TV area"
FPS = 25
SMOOTHING = 0.35       # 0 = no smoothing, closer to 1 = slower fades
SPREAD = 0.55          # how wide each lamp samples the Ambilight zones (in x units)
DARK_TIMEOUT = 15      # seconds of black Ambilight before the lights are released
BRIGHTNESS = 1.0       # global multiplier for the colours sent to Hue


def load_config():
    if not CONFIG.exists():
        sys.exit(f"{CONFIG} not found: pair the Hue bridge and the TV first (see README)")
    cfg = json.loads(CONFIG.read_text())
    for key in ("hue", "tv"):
        if key not in cfg:
            sys.exit(f"'{key}' missing in {CONFIG}: run pair_{key}.py first (see README)")
    return cfg


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Hue:
    def __init__(self, ip, username, clientkey):
        self.ip, self.username, self.clientkey = ip, username, clientkey
        self.ctx = ssl._create_unverified_context()  # bridge uses a self-signed certificate
        self.proc = None
        self.seq = 0

    def _api(self, method, path, body=None):
        req = urllib.request.Request(
            f"https://{self.ip}/clip/v2/resource/{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"hue-application-key": self.username, "Content-Type": "application/json"},
        )
        return json.loads(urllib.request.urlopen(req, context=self.ctx, timeout=5).read())

    def find_area(self, name):
        for area in self._api("GET", "entertainment_configuration")["data"]:
            if area["metadata"]["name"] == name:
                return area
        sys.exit(f"Entertainment area '{name}' not found on the bridge")

    def start(self, area_id):
        self._api("PUT", f"entertainment_configuration/{area_id}", {"action": "start"})
        # Python's ssl module has no DTLS, so the stream runs through openssl s_client.
        self.proc = subprocess.Popen(
            [OPENSSL, "s_client", "-quiet", "-nocommands", "-dtls1_2",
             "-cipher", "PSK-AES128-GCM-SHA256",
             "-psk_identity", self.username, "-psk", self.clientkey,
             "-connect", f"{self.ip}:2100"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        time.sleep(1.0)  # let the DTLS handshake finish before the first frame
        if self.proc.poll() is not None:
            err = self.proc.stderr.read().decode(errors="replace")
            self.proc = None
            self._api("PUT", f"entertainment_configuration/{area_id}", {"action": "stop"})
            raise RuntimeError(f"DTLS handshake failed: {err.strip()[-300:]}")

    def stop(self, area_id):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=3)
            self.proc = None
        try:
            self._api("PUT", f"entertainment_configuration/{area_id}", {"action": "stop"})
        except OSError:
            pass

    @property
    def streaming(self):
        return self.proc is not None and self.proc.poll() is None

    def send(self, area_id, colours):
        """colours: {channel_id: (r, g, b)} with components in 0..1."""
        msg = bytearray(b"HueStream")
        msg += bytes([2, 0, self.seq, 0, 0, 0, 0])  # version 2.0, sequence, reserved, RGB, reserved
        msg += area_id.encode()
        for ch, (r, g, b) in colours.items():
            msg.append(ch)
            for v in (r, g, b):
                msg += int(max(0.0, min(1.0, v)) * 65535).to_bytes(2, "big")
        self.seq = (self.seq + 1) % 256
        self.proc.stdin.write(msg)
        self.proc.stdin.flush()


def zone_layout(topology):
    """x position (-1 left .. 1 right, viewer's perspective) of every Ambilight zone."""
    zones = []
    for side, x in (("left", -1.0), ("right", 1.0)):
        zones += [(side, str(i), x) for i in range(topology.get(side, 0))]
    top = topology.get("top", 0)
    zones += [("top", str(i), -1 + (2 * i + 1) / top) for i in range(top)]
    bottom = topology.get("bottom", 0)
    zones += [("bottom", str(i), -1 + (2 * i + 1) / bottom) for i in range(bottom)]
    return zones


def channel_weights(channels, zones):
    """Per Hue channel, a normalised Gaussian weight for every Ambilight zone based on x."""
    weights = {}
    for ch in channels:
        x = ch["position"]["x"]
        w = [math.exp(-((zx - x) ** 2) / (2 * SPREAD ** 2)) for _, _, zx in zones]
        total = sum(w)
        weights[ch["channel_id"]] = [v / total for v in w]
    return weights


def mix(measured, zones, weights):
    layer = measured.get("layer1", {})
    rgb = [tuple(layer.get(side, {}).get(idx, {}).get(c, 0) / 255 for c in "rgb") for side, idx, _ in zones]
    out = {}
    for ch, w in weights.items():
        out[ch] = tuple(sum(wi * px[k] for wi, px in zip(w, rgb)) for k in range(3))
    return out


def is_dark(colours, threshold=0.02):
    return all(max(c) < threshold for c in colours.values())


def run(tv, hue, area, enabled=lambda: True, handle_signals=True):
    """Stream Ambilight to the area. `enabled` lets a controller (bridge.py) switch the sync on and off."""
    area_id = area["id"]
    zones = None
    weights = None
    smoothed = {}
    last_light = 0.0
    frame = 1.0 / FPS

    def shutdown(*_):
        log("stopping")
        hue.stop(area_id)
        tv.close()
        sys.exit(0)

    if handle_signals:
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
    log(f"waiting for TV, streaming to '{area['metadata']['name']}' ({len(area['channels'])} channels)")

    while True:
        t0 = time.monotonic()
        if not enabled():
            if hue.streaming:
                log("sync switched off, releasing lights")
                hue.stop(area_id)
            time.sleep(0.5)
            continue
        try:
            if zones is None:
                zones = zone_layout(tv.get("ambilight/topology"))
                weights = channel_weights(area["channels"], zones)
                log(f"TV reachable, {len(zones)} Ambilight zones")
            target = mix(tv.get("ambilight/measured"), zones, weights)
        except (OSError, RuntimeError) as e:
            if hue.streaming:
                log(f"TV unreachable ({e.__class__.__name__}), releasing lights")
                hue.stop(area_id)
            zones = None
            tv.close()
            time.sleep(5)
            continue

        now = time.monotonic()
        if not is_dark(target):
            last_light = now
            if not hue.streaming:
                log("Ambilight active, starting stream")
                try:
                    hue.start(area_id)
                except (OSError, RuntimeError) as e:
                    log(f"could not start stream: {e}")
                    time.sleep(5)
                    continue
                smoothed = dict(target)
        elif hue.streaming and now - last_light > DARK_TIMEOUT:
            log("Ambilight dark, releasing lights")
            hue.stop(area_id)

        if hue.streaming:
            for ch, rgb in target.items():
                prev = smoothed.get(ch, rgb)
                smoothed[ch] = tuple(SMOOTHING * p + (1 - SMOOTHING) * c for p, c in zip(prev, rgb))
            try:
                hue.send(area_id, {ch: tuple(v * BRIGHTNESS for v in rgb) for ch, rgb in smoothed.items()})
            except (BrokenPipeError, OSError):
                log("stream dropped, restarting")
                hue.stop(area_id)

        time.sleep(max(0.0, frame - (time.monotonic() - t0)))


def test(hue, area):
    """Light each channel red/green/blue in turn so positions can be checked."""
    area_id = area["id"]
    hue.start(area_id)
    try:
        channels = [ch["channel_id"] for ch in area["channels"]]
        for ch in channels:
            log(f"channel {ch} at x={area['channels'][ch]['position']['x']:+.2f} -> red, others off")
            end = time.monotonic() + 3
            while time.monotonic() < end:
                hue.send(area_id, {c: (1, 0, 0) if c == ch else (0, 0, 0) for c in channels})
                time.sleep(1 / FPS)
    finally:
        hue.stop(area_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", action="store_true", help="flash each Hue channel to check positions")
    args = parser.parse_args()

    cfg = load_config()
    hue = Hue(cfg["hue"]["ip"], cfg["hue"]["username"], cfg["hue"]["clientkey"])
    area = hue.find_area(cfg.get("entertainment_area", DEFAULT_AREA))
    if args.test:
        test(hue, area)
        return
    tv = TVClient(cfg["tv"]["ip"], cfg["tv"]["device_id"], cfg["tv"]["auth_key"])
    run(tv, hue, area)


if __name__ == "__main__":
    main()
