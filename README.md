# ambilight-hue-bridge

Make your Philips Hue lights follow your Philips Ambilight TV, with no HDMI box and no camera. The bridge reads the colours the TV already measures for Ambilight and streams them to a Hue entertainment area at 25 fps.

It also has an optional **cinema autopilot** for an Apple TV. When a video starts, the room dims and the sync takes over. When you pause, the lights come up softly. When playback stops, every light goes back to how it was.

## Requirements

| | |
|---|---|
| TV | Philips Android TV with Ambilight and JointSpace API v6 (most 2016+ models) |
| Hue | Hue Bridge (v2, square) with an **entertainment area** set up in the Hue app |
| Apple TV | optional, for the autopilot |
| Host | anything on the same LAN that runs Docker (or Python 3.11 + `openssl`), e.g. a home server or Raspberry Pi |

## Quick start (Docker)

```bash
git clone https://github.com/gavinguler/ambilight-hue-bridge && cd ambilight-hue-bridge
mkdir data

# 1. Hue Bridge: run this, then press the round link button on the bridge
docker compose run --rm bridge python pair_hue.py 192.168.1.20

# 2. TV: a PIN appears on the TV, then grant it
docker compose run --rm bridge python pair_tv.py request 192.168.1.30
docker compose run --rm bridge python pair_tv.py grant 1234

# 3. Optional: Apple TV (a PIN appears on the TV twice: AirPlay and Companion)
docker compose run --rm bridge python pair_atv.py scan
docker compose run --rm -it bridge python pair_atv.py pair <identifier>

# 4. Check that the lights are in the right positions, then run it
docker compose run --rm bridge python sync.py --test
docker compose up -d
```

Everything the pairing writes ends up in `./data`: `config.json` and `.pyatv.conf`. Keep that folder private.

The container needs `network_mode: host`, because Apple TV discovery uses mDNS multicast.

## Configuration

`data/config.json` (see [`config.example.json`](config.example.json)):

| Key | Default | |
|-----|---------|---|
| `hue` | — | written by `pair_hue.py` |
| `tv` | — | written by `pair_tv.py` |
| `entertainment_area` | `TV area` | name of the Hue entertainment area to stream to |
| `room` | `Living room` | Hue room whose lights the scenes and autopilot control |
| `apple_tv.id` | — | optional; written by `pair_atv.py` |

Lights in the entertainment area follow the picture. The other lights in the room get the scene. You can tune smoothing, spread, fps and brightness at the top of `sync.py`. The scene looks are set at the top of `bridge.py`.

| Environment variable | Default | |
|-----|---------|---|
| `DATA_DIR` | script folder (`/data` in Docker) | where `config.json` and state live |
| `PORT` | `8765` | API port |
| `OPENSSL` | auto | path to an `openssl` binary with DTLS 1.2 + PSK |

## HTTP API

| Method | Path | |
|--------|------|---|
| `GET` | `/status` | what is playing, current mode, autopilot and sync state |
| `GET` | `/artwork` | artwork of what the Apple TV is playing |
| `POST` | `/scene/film` | dim the room and start the sync |
| `POST` | `/scene/cozy` | warm, soft light; sync off |
| `POST` | `/scene/off` | lights off and TV to standby |
| `POST` | `/autopilot?on=0` | switch the autopilot off (`on=1` to switch it back on) |

## Running without Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python pair_hue.py <bridge-ip>
.venv/bin/python pair_tv.py request <tv-ip>  &&  .venv/bin/python pair_tv.py grant <PIN>
.venv/bin/python bridge.py        # or: sync.py for the sync only, without autopilot/API
```

On macOS the bundled LibreSSL cannot do DTLS-PSK, so install OpenSSL first with `brew install openssl@3`. The bridge finds it automatically.

## How it works

- **TV**: `tvclient.py` keeps a single keep-alive HTTPS connection to the JointSpace API (`/6/ambilight/measured`), because the TV's TLS stack stalls on rapid new connections.
- **Hue**: the bridge starts the entertainment area over CLIP v2. Python's `ssl` module has no DTLS, so the HueStream v2 frames go through `openssl s_client -dtls1_2 -psk`.
- **Mapping**: every Hue channel takes a Gaussian-weighted mix of the Ambilight zones near its x position, smoothed over time. The lights are released after 15 s of black.

## License

[MIT](LICENSE)

Not affiliated with Philips, Signify or Apple. Philips, Ambilight and Hue are trademarks of their respective owners.
