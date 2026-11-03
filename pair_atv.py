"""Optional: pair an Apple TV for the cinema autopilot (pyatv).

    python3 pair_atv.py scan                    list Apple TVs on the network
    python3 pair_atv.py pair <identifier>       pair AirPlay + Companion; the TV shows a PIN for each

The identifier is stored in config.json, the pairing credentials in ~/.pyatv.conf
(in Docker: /data/.pyatv.conf, because HOME=/data).
"""
import asyncio
import sys

import pyatv
from pyatv.const import Protocol
from pyatv.storage.file_storage import FileStorage

from pair_tv import load_config, save_config


async def scan():
    for conf in await pyatv.scan(asyncio.get_running_loop(), timeout=5):
        print(f"{conf.name:30} {conf.address}   identifier: {conf.identifier}")


async def pair(identifier):
    loop = asyncio.get_running_loop()
    storage = FileStorage.default_storage(loop)
    await storage.load()
    for protocol in (Protocol.AirPlay, Protocol.Companion):
        confs = await pyatv.scan(loop, identifier=identifier, storage=storage, timeout=5)
        if not confs:
            sys.exit("Apple TV not found; run 'pair_atv.py scan' and check the identifier")
        pairing = await pyatv.pair(confs[0], protocol, loop, storage=storage, name="Ambilight Hue Bridge")
        await pairing.begin()
        pairing.pin(input(f"[{protocol.name}] PIN shown on the TV: ").strip())
        await pairing.finish()
        print(f"[{protocol.name}] paired: {pairing.has_paired}")
        await pairing.close()
    await storage.save()
    cfg = load_config()
    cfg["apple_tv"] = {"id": identifier}
    save_config(cfg)
    print("Apple TV stored in config.json")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "scan":
        asyncio.run(scan())
    elif len(sys.argv) == 3 and sys.argv[1] == "pair":
        asyncio.run(pair(sys.argv[2]))
    else:
        sys.exit(__doc__)
