"""Ambilight -> Hue bridge with an Apple TV aware cinema autopilot and a small HTTP API.

- Follows the Apple TV (pyatv, paired in ~/.pyatv.conf) if one is configured: app, title, playing/paused, artwork.
- Cinema autopilot: video starts -> the room dims and the Ambilight->Hue sync runs; pause -> lights come up
  softly; playback stops -> everything returns to how it was before.
- Scenes on demand: film, cozy, off (lights off + TV to standby).

    python3 bridge.py        API on http://0.0.0.0:8765
"""
import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pyatv
from aiohttp import web
from pyatv.const import DeviceState, MediaType, PowerState
from pyatv.storage.file_storage import FileStorage

import sync
from sync import Hue, log
from tvclient import TVClient

DEFAULT_ROOM = "Living room"
PORT = int(os.environ.get("PORT", 8765))
POLL_S = 1.5
STOP_GRACE_S = 30      # idle this long after a film before the lights are restored
STATE_FILE = sync.DATA_DIR / "bridge_state.json"

# Light looks per scene: (brightness %, mirek) — mirek 454 ≈ 2200 K, 417 ≈ 2400 K, 400 = 2500 K.
FILM = (10, 454)
PAUSE = (40, 417)
COZY = (60, 400)


class Lights:
    """The room's Hue lights: scenes, plus a snapshot to put everything back after a film."""

    def __init__(self, hue: Hue, area: dict, room_name: str):
        self.hue = hue
        devices = {d["id"]: d for d in hue._api("GET", "device")["data"]}
        rooms = hue._api("GET", "room")["data"]
        room = next((r for r in rooms if r["metadata"]["name"] == room_name), None)
        if room is None:
            names = ", ".join(r["metadata"]["name"] for r in rooms)
            raise SystemExit(f"Room '{room_name}' not found on the bridge (rooms: {names}); set \"room\" in config.json")
        ids = [s["rid"] for ch in room["children"] if ch["rtype"] == "device"
               for s in devices.get(ch["rid"], {}).get("services", []) if s["rtype"] == "light"]
        lights = {l["id"]: l for l in hue._api("GET", "light")["data"]}
        self.lights = {i: lights[i] for i in ids if i in lights}
        # Lights in the entertainment area follow the picture while the sync runs; the rest get the scene.
        self.synced = {s["rid"] for s in area.get("light_services", [])}
        self.snapshot = None
        names = ", ".join(l["metadata"]["name"] for l in self.lights.values())
        log(f"{room_name}: {names} ({len(self.synced)} follow the TV)")

    def _put(self, light_id, body):
        try:
            self.hue._api("PUT", f"light/{light_id}", body)
        except OSError as e:
            log(f"light {light_id[:8]}: {e}")
        time.sleep(0.12)  # stay under the bridge's ~10 commands/s

    def take_snapshot(self):
        current = {l["id"]: l for l in self.hue._api("GET", "light")["data"]}
        self.snapshot = {i: current[i] for i in self.lights if i in current}

    def restore(self):
        if not self.snapshot:
            return
        for light_id, l in self.snapshot.items():
            body = {"on": {"on": l["on"]["on"]}}
            if l["on"]["on"] and "dimming" in l:
                body["dimming"] = {"brightness": l["dimming"]["brightness"]}
                ct = l.get("color_temperature") or {}
                if ct.get("mirek_valid") and ct.get("mirek"):
                    body["color_temperature"] = {"mirek": ct["mirek"]}
                elif "color" in l:
                    body["color"] = {"xy": l["color"]["xy"]}
            self._put(light_id, body)
        self.snapshot = None

    def look(self, look, include_synced=False):
        brightness, mirek = look
        for light_id, l in self.lights.items():
            if light_id in self.synced and not include_synced:
                continue
            body = {"on": {"on": True}, "dimming": {"brightness": brightness}}
            if "color_temperature" in l:
                body["color_temperature"] = {"mirek": mirek}
            self._put(light_id, body)

    def off(self):
        for light_id in self.lights:
            self._put(light_id, {"on": {"on": False}})


class Bridge:
    def __init__(self):
        cfg = sync.load_config()
        self.hue = Hue(cfg["hue"]["ip"], cfg["hue"]["username"], cfg["hue"]["clientkey"])
        self.area = self.hue.find_area(cfg.get("entertainment_area", sync.DEFAULT_AREA))
        self.lights = Lights(self.hue, self.area, cfg.get("room", DEFAULT_ROOM))
        self.tv_cfg = cfg["tv"]
        self.atv_id = (cfg.get("apple_tv") or {}).get("id")  # optional
        saved = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        self.autopilot = saved.get("autopilot", True)
        self.sync_on = False
        self.mode = "normal"             # normal | film | pause | cozy | off
        self.source = None               # "auto" or "manual" while in film/pause
        self.suppress = False            # a manual scene wins until the current playback stops
        self.idle_since = None
        self.now = {"app": None, "app_id": None, "state": "off", "media": None, "title": None, "subtitle": None}
        self.artwork = None              # (bytes, mimetype)
        self.artwork_key = None
        self.artwork_version = 0
        self.lock = asyncio.Lock()

    # ---------- Apple TV ----------

    async def follow_apple_tv(self):
        if not self.atv_id:
            log("no Apple TV configured: autopilot idle, scenes via the API only")
            await asyncio.Event().wait()
        loop = asyncio.get_running_loop()
        storage = FileStorage.default_storage(loop)
        await storage.load()
        while True:
            atv = None
            try:
                confs = await pyatv.scan(loop, identifier=self.atv_id, storage=storage, timeout=5)
                if not confs:
                    raise ConnectionError("Apple TV not found")
                atv = await pyatv.connect(confs[0], loop, storage=storage)
                log(f"connected to Apple TV '{confs[0].name}'")
                while True:
                    await self.read_apple_tv(atv)
                    await self.autopilot_step()
                    await asyncio.sleep(POLL_S)
            except Exception as e:  # network drops, Apple TV asleep, protocol errors: reconnect
                log(f"Apple TV: {e.__class__.__name__}: {e}")
                self.now.update(state="off", title=None, subtitle=None)
                await self.autopilot_step()
                await asyncio.sleep(10)
            finally:
                if atv:
                    atv.close()

    async def read_apple_tv(self, atv):
        power = atv.power.power_state
        if power == PowerState.Off:
            self.now.update(state="off", app=None, app_id=None, media=None, title=None, subtitle=None)
            return
        playing = await atv.metadata.playing()
        try:
            app = atv.metadata.app
        except Exception:
            app = None
        state = {DeviceState.Playing: "playing", DeviceState.Paused: "paused",
                 DeviceState.Loading: "playing", DeviceState.Seeking: "playing"}.get(playing.device_state, "idle")
        subtitle = None
        if playing.series_name:
            subtitle = playing.series_name
            if playing.season_number and playing.episode_number:
                subtitle += f" · S{playing.season_number} A{playing.episode_number}"
        elif playing.artist:
            subtitle = playing.artist
        self.now.update(
            state=state, media=playing.media_type.name.lower(), title=playing.title, subtitle=subtitle,
            app=app.name if app else None, app_id=app.identifier if app else None,
        )
        key = (playing.title, playing.series_name, playing.artist, self.now["app_id"])
        if key != self.artwork_key and state != "idle":
            self.artwork_key = key
            try:
                art = await atv.metadata.artwork(width=640)
            except Exception:
                art = None
            self.artwork = (art.bytes, art.mimetype) if art and art.bytes else None
            self.artwork_version += 1

    # ---------- autopilot ----------

    def playing_video(self):
        return self.now["state"] == "playing" and self.now["media"] != MediaType.Music.name.lower()

    async def autopilot_step(self):
        state = self.now["state"]
        stopped = state in ("idle", "off")
        now = time.monotonic()
        self.idle_since = (self.idle_since or now) if stopped else None
        if stopped and self.suppress and now - self.idle_since > 5:
            self.suppress = False  # playback ended: the autopilot may take over again next time

        async with self.lock:
            if self.mode in ("film", "pause"):
                if self.mode == "film" and state == "paused":
                    await self.apply("pause")
                elif self.mode == "pause" and self.playing_video():
                    await self.apply("film-resume")
                elif stopped and (state == "off" or (self.source == "auto" and now - self.idle_since > STOP_GRACE_S)):
                    log("playback stopped: restoring the room")
                    await self.apply("restore")
            elif self.autopilot and not self.suppress and self.playing_video():
                log(f"cinema: {self.now['app']} · {self.now['title'] or 'video'}")
                await self.apply("film", source="auto")

    async def apply(self, action, source=None):
        """Run a light change off the event loop (Hue calls block)."""
        run = asyncio.get_running_loop().run_in_executor

        if action == "film":
            if self.mode not in ("film", "pause"):
                await run(None, self.lights.take_snapshot)
            self.source = source or "manual"
            self.sync_on = True
            await run(None, self.lights.look, FILM)
            self.mode = "film"
        elif action == "film-resume":
            await run(None, self.lights.look, FILM)
            self.mode = "film"
        elif action == "pause":
            await run(None, self.lights.look, PAUSE)
            self.mode = "pause"
        elif action == "restore":
            self.sync_on = False
            await asyncio.sleep(1.0)  # let the sync thread release the entertainment area first
            await run(None, self.lights.restore)
            self.mode, self.source = "normal", None
        elif action == "cozy":
            self.sync_on = False
            self.lights.snapshot = None
            await asyncio.sleep(1.0)
            await run(None, self.lights.look, COZY, True)
            self.mode, self.source = "cozy", None
            self.suppress = self.now["state"] in ("playing", "paused")
        elif action == "off":
            self.sync_on = False
            self.lights.snapshot = None
            await asyncio.sleep(1.0)
            await run(None, self.lights.off)
            await run(None, self.tv_standby)
            self.mode, self.source = "off", None
            self.suppress = self.now["state"] in ("playing", "paused")
        log(f"scene: {self.mode}{' (sync on)' if self.sync_on else ''}")

    def tv_standby(self):
        tv = TVClient(self.tv_cfg["ip"], self.tv_cfg["device_id"], self.tv_cfg["auth_key"])
        try:
            tv.post("powerstate", {"powerstate": "Standby"})
        except (OSError, RuntimeError) as e:
            log(f"tv standby: {e}")
        finally:
            tv.close()

    # ---------- Hue sync ----------

    def start_sync_thread(self):
        tv = TVClient(self.tv_cfg["ip"], self.tv_cfg["device_id"], self.tv_cfg["auth_key"])
        threading.Thread(
            target=sync.run, args=(tv, self.hue, self.area),
            kwargs={"enabled": lambda: self.sync_on, "handle_signals": False},
            daemon=True, name="sync",
        ).start()

    # ---------- API ----------

    def status(self):
        return {
            **self.now,
            "artwork": f"/artwork?v={self.artwork_version}" if self.artwork else None,
            "mode": self.mode, "autopilot": self.autopilot, "sync": self.sync_on,
        }

    async def handle_status(self, _request):
        return web.json_response(self.status())

    async def handle_artwork(self, _request):
        if not self.artwork:
            raise web.HTTPNotFound()
        data, mime = self.artwork
        return web.Response(body=data, content_type=mime or "image/jpeg", headers={"Cache-Control": "max-age=3600"})

    async def handle_scene(self, request):
        name = request.match_info["name"]
        if name not in ("film", "cozy", "off"):
            raise web.HTTPNotFound(text="unknown scene")
        async with self.lock:
            await self.apply(name, source="manual")
        return web.json_response(self.status())

    async def handle_autopilot(self, request):
        self.autopilot = request.query.get("on", "1") not in ("0", "false", "off")
        STATE_FILE.write_text(json.dumps({"autopilot": self.autopilot}))
        log(f"autopilot {'on' if self.autopilot else 'off'}")
        return web.json_response(self.status())


async def main():
    bridge = Bridge()
    bridge.start_sync_thread()
    app = web.Application()
    app.add_routes([
        web.get("/status", bridge.handle_status),
        web.get("/artwork", bridge.handle_artwork),
        web.post("/scene/{name}", bridge.handle_scene),
        web.post("/autopilot", bridge.handle_autopilot),
    ])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log(f"API on http://0.0.0.0:{PORT} · autopilot {'on' if bridge.autopilot else 'off'}")
    await bridge.follow_apple_tv()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
