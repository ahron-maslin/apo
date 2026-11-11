"""Screenshot the demo dashboard for the website: each page 1024 CSS px wide, as tall as
its content, in light and dark, at the four widths the site's srcset lists (768, 1080,
1440 and the 2048 original), straight to WebP. Prints each page's CSS height for the
<img> width/height attributes in site/index.html.

    shoot.py BASE_URL OUT_DIR"""
import asyncio
import base64
import http.client
import os
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import websockets

SHELL = Path(os.environ.get("APO_CHROME") or
             Path.home() / ".cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell")
BASE, OUT = sys.argv[1], Path(sys.argv[2])
WIDTH = 1024
# page: (path, the element whose bottom ends the shot, or None for the whole page). The site
# crops Now and Agents itself (crop-now, crop-agents) and links to the whole image.
PAGES = {"now": ("/", None), "round": ("/round?minutes=15", None), "agents": ("/projects/agents", None),
         "goal": ("/outcomes/1", ".checklist-steps")}
SIZES = {"": 2048, "-1440": 1440, "-1080": 1080, "-768": 768}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port: int, path: str) -> bytes:
    for _ in range(50):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", path)
            return conn.getresponse().read()
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(path)


async def main() -> None:
    port = free_port()
    browser = subprocess.Popen([str(SHELL), "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                                f"--remote-debugging-port={port}", "about:blank"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = next(t for t in json.loads(get(port, "/json")) if t["type"] == "page")
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=200_000_000) as ws:
            counter = 0

            async def call(method, **params):
                nonlocal counter
                counter += 1
                await ws.send(json.dumps({"id": counter, "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == counter:
                        if "error" in msg:
                            raise RuntimeError(f"{method}: {msg['error']}")
                        return msg.get("result", {})

            async def js(expr):
                return (await call("Runtime.evaluate", expression=expr, returnByValue=True)).get("result", {}).get("value")

            await call("Page.enable")
            for name, (path, until) in PAGES.items():
                for theme in ("light", "dark"):
                    await call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": theme}])
                    height = None
                    for suffix, pixels in SIZES.items():
                        scale = pixels / WIDTH
                        await call("Emulation.setDeviceMetricsOverride", width=WIDTH, height=900, deviceScaleFactor=scale, mobile=False)
                        await call("Page.navigate", url=BASE + path)
                        await asyncio.sleep(1.5)
                        height = await js(f"Math.ceil(document.querySelector({json.dumps(until)}).getBoundingClientRect().bottom + 48)"
                                          if until else "Math.ceil(document.documentElement.scrollHeight)")
                        await call("Emulation.setDeviceMetricsOverride", width=WIDTH, height=height, deviceScaleFactor=scale, mobile=False)
                        await asyncio.sleep(0.4)
                        shot = await call("Page.captureScreenshot", format="webp", quality=82)
                        (OUT / f"{name}-{theme}{suffix}.webp").write_bytes(base64.b64decode(shot["data"]))
                    print(f"{name} {theme}: {WIDTH}x{height}")
    finally:
        browser.terminate()


asyncio.run(main())
