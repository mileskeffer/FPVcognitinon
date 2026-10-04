r"""Browser control for the Air75 Wi-Fi bridge, meant to run on a Raspberry Pi.

Network layout:
  [ phone / laptop browser ] --(Pi's onboard Wi-Fi or Ethernet)--> [ Pi: this server ]
  [ Pi: USB Wi-Fi (e.g. wlan1) ] --(joined to Air75-Control)--> [ drone ESP ] --> Betaflight

Run on the Pi:
  pip install aiohttp
  sudo python3 web_control.py --iface wlan1      (sudo only needed for --iface)
Then open http://<pi-ip>:8080 from any browser that can reach the Pi.

Run on Windows with the TL-WN725N connected to Air75-Control:
  .\.venv\Scripts\python.exe web_control.py --bind-ip 192.168.4.2
Then open http://127.0.0.1:8080 on the same PC.

Video: run MediaMTX alongside this (./mediamtx mediamtx.yml). The page embeds its
WebRTC player from http://<same host>:8889/cam/ unless --video-url says otherwise.

Safety behavior:
  - Movement buttons are hold-to-move. Let go and the sticks re-center (angle mode self-levels).
  - The browser sends a heartbeat 20x/sec. If it stops for 0.5 s (tab closed, phone
    locked, Wi-Fi to the Pi dropped), the Pi stops talking to the drone and the
    ESP stops CRSF and Betaflight runs its configured failsafe.
  - If the Pi crashes or the USB Wi-Fi drops, Betaflight detects RX loss.
  - KILL is instant motor-off. It will fall.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from dronelink import DroneLink

HERE = Path(__file__).resolve().parent

CLIENT_TIMEOUT = 0.5   # s without a browser heartbeat before we let go of the drone
TICK = 0.02            # control loop period (50 Hz)
STATUS_PERIOD = 0.1    # status push to browsers (10 Hz)

TAKEOFF_SPOOL = 0.6    # s at idle after arming before throttle ramps up
TAKEOFF_RAMP = 1.5     # s to ramp from idle to hover throttle
LAND_RAMP = 3.0        # s to ramp down before disarming
LAND_FLOOR = 1150      # throttle at the end of the landing ramp
CLIMB_STEP = 60        # throttle added/removed while Up/Down is held
YAW_RATE = 120         # yaw stick deflection while a yaw button is held


def clamp(v, lo, hi):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


class Controller:
    """Turns browser intents (hold forward, climb, etc.) into stick values."""

    def __init__(self, link):
        self.link = link
        self.mode = "idle"            # idle, takeoff, fly, land
        self.mode_t = 0.0
        self.hover = 1350             # tune this live from the page
        self.tilt = 80                # roll/pitch deflection; angle mode: 500 = max tilt
        self.intent = {"pitch": 0, "roll": 0, "yaw": 0, "climb": 0}
        self.throttle = 1000
        self.land_from = 1000
        self.last_client = 0.0
        self.note = "ready"

    # ----- input from browsers -----
    def client_msg(self, msg):
        self.last_client = time.monotonic()   # any message counts as a heartbeat
        kind = msg.get("type")
        if kind == "intent":
            for k in self.intent:
                self.intent[k] = clamp(msg.get(k, 0), -1, 1)
        elif kind == "settings":
            if "hover" in msg:
                self.hover = clamp(msg["hover"], 1150, 1700)
            if "tilt" in msg:
                self.tilt = clamp(msg["tilt"], 0, 200)
        elif kind == "cmd":
            self.command(msg.get("cmd"))

    def command(self, cmd):
        now = time.monotonic()
        if cmd == "takeoff":
            if self.mode != "idle":
                return
            drone = self.drone_state()
            if drone != "DISARMED":
                self.note = f"can't take off, drone is {drone}"
                return
            self.throttle = 1000
            self.link.set_sticks(roll=1500, pitch=1500, yaw=1500, throttle=1000)
            if not self.link.arm():
                self.note = "arm refused"
                return
            self.mode, self.mode_t = "takeoff", now
            self.note = "taking off"
        elif cmd == "land":
            if self.mode in ("takeoff", "fly"):
                self.mode, self.mode_t = "land", now
                self.land_from = self.throttle
                self.note = "landing"
        elif cmd == "kill":
            self.link.kill()
            self._go_idle("KILLED")
        elif cmd == "reset":
            self._go_idle("reset")

    def _go_idle(self, note):
        self.mode = "idle"
        self.throttle = 1000
        self.note = note
        self.link.disarm()

    # ----- 50 Hz control loop -----
    def tick(self):
        now = time.monotonic()

        # Browser gone: stop talking to the drone FIRST so it never sees a disarm
        # (which would drop it), then let Betaflight handle RX-loss failsafe.
        if now - self.last_client > CLIENT_TIMEOUT:
            if not self.link.paused:
                self.link.pause()
                if self.mode != "idle":
                    self._go_idle("browser lost: Betaflight failsafe active")
            for k in self.intent:
                self.intent[k] = 0
            return
        if self.link.paused:
            self.link.resume()

        # Drone side failsafe happened (USB Wi-Fi dropped, etc.): stand down. Sending
        # disarm here also clears the firmware latch once it finishes descending.
        drone = self.drone_state()
        if self.mode != "idle" and drone == "FAILSAFE_LATCHED":
            self._go_idle(f"drone failsafe ({drone}); reconnect disarmed before arming again")

        thr = 1000
        if self.mode == "takeoff":
            e = now - self.mode_t
            if e < TAKEOFF_SPOOL:
                thr = 1000
            elif e < TAKEOFF_SPOOL + TAKEOFF_RAMP:
                thr = 1000 + (self.hover - 1000) * (e - TAKEOFF_SPOOL) / TAKEOFF_RAMP
            else:
                self.mode, self.note = "fly", "flying"
                thr = self.hover
        elif self.mode == "fly":
            thr = self.hover + CLIMB_STEP * self.intent["climb"]
        elif self.mode == "land":
            e = now - self.mode_t
            floor = min(LAND_FLOOR, self.land_from)
            if e >= LAND_RAMP:
                self._go_idle("landed")
                thr = 1000
            else:
                thr = self.land_from - (self.land_from - floor) * e / LAND_RAMP

        self.throttle = int(thr)
        moving = self.mode == "fly"
        self.link.set_sticks(
            roll=1500 + (self.tilt * self.intent["roll"] if moving else 0),
            pitch=1500 + (self.tilt * self.intent["pitch"] if moving else 0),
            yaw=1500 + (YAW_RATE * self.intent["yaw"] if moving else 0),
            throttle=self.throttle,
        )

    def drone_state(self):
        return self.link.status if self.link.link_ok else "NO LINK"

    def status(self):
        return {
            "drone": self.drone_state(),
            "mode": self.mode,
            "throttle": self.throttle,
            "hover": self.hover,
            "tilt": self.tilt,
            "note": self.note,
        }


def build_app(link, video_url=""):
    ctl = Controller(link)
    clients = set()

    async def index(_req):
        return web.FileResponse(HERE / "control.html")

    async def config(_req):
        # Empty video_url = page uses MediaMTX on the same host: http://<host>:8889/cam/
        return web.json_response({"video_url": video_url})

    async def ws_handler(req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if isinstance(data, dict):
                            ctl.client_msg(data)
                    except ValueError:
                        pass
        finally:
            clients.discard(ws)
            if not clients:
                # Socket closed cleanly: stop moving right away instead of waiting
                # out the heartbeat timeout. Throttle stays at hover until then.
                for k in ctl.intent:
                    ctl.intent[k] = 0
        return ws

    async def control_loop():
        while True:
            ctl.tick()
            await asyncio.sleep(TICK)

    async def status_loop():
        while True:
            payload = json.dumps(ctl.status())
            for ws in list(clients):
                try:
                    await ws.send_str(payload)
                except (ConnectionError, RuntimeError):
                    clients.discard(ws)
            await asyncio.sleep(STATUS_PERIOD)

    async def lifecycle(_app):
        link.pause()   # stay silent until a browser connects
        link.start()
        if link.wifi_message:
            print(link.wifi_message)
        tasks = [asyncio.create_task(control_loop()), asyncio.create_task(status_loop())]
        yield
        for t in tasks:
            t.cancel()
        link.stop()   # stops sending so Betaflight detects RX loss

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/config.json", config)
    app.cleanup_ctx.append(lifecycle)
    return app


def main():
    ap = argparse.ArgumentParser(description="Browser control for the Air75 Wi-Fi bridge")
    ap.add_argument("--host", default="0.0.0.0", help="address to serve the page on")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--drone-ip", default="192.168.4.1")
    ap.add_argument("--bind-ip", default=None,
                    help="local adapter address, e.g. 192.168.4.2 on Windows")
    ap.add_argument("--iface", default=None, help="pin drone traffic to this interface, e.g. wlan1 (needs root)")
    ap.add_argument("--video-url", default="",
                    help="video player URL to embed; default is MediaMTX on this host at :8889/cam/")
    args = ap.parse_args()

    link = DroneLink(ip=args.drone_ip, bind_ip=args.bind_ip, iface=args.iface)
    if link.bind_warning:
        print(f"WARNING: {link.bind_warning}")
    web.run_app(build_app(link, args.video_url), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
