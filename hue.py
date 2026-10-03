#!/usr/bin/python3
"""Tiny Philips Hue controller (Hue API v2, stdlib only).

  ./hue.py discover              find bridges on your network
  ./hue.py pair hue [IP]         press the Hue bridge button, then run this
  ./hue.py pair ikea IP          IKEA DIRIGERA hub (press its action button when asked)
  ./hue.py lights                list every light
  ./hue.py on | off [NAME...]    all lights, or only the ones matching NAME
  ./hue.py rename OLD NEW        rename a Hue light on the bridge
  ./hue.py set [NAME...] -b 30 -c "#ff8800" | -k 2700
  ./hue.py lava [--room Living]  slow lava-lamp blobs of color (per light, unsynced)
  ./hue.py night [options]       slowly drifting ambient night lighting
"""
import argparse, base64, colorsys, hashlib, json, math, os, secrets, signal, ssl, sys, time
import urllib.parse, urllib.request

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".hue.json")

# Dim, warm, night-friendly palette (RGB). The drift loops through these.
PALETTE = [
    (255, 110, 20),   # deep amber
    (255, 60, 40),    # ember red
    (200, 40, 110),   # dusky rose
    (110, 40, 190),   # twilight violet
    (30, 70, 220),    # night blue
    (20, 130, 170),   # deep teal
    (255, 140, 50),   # back toward amber
]

# Bridge uses a private CA; we're talking to a LAN device by IP, so skip verification.
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def http(method, url, body=None, headers=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, context=_ctx, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


class HueLight:
    color = False

    def __init__(self, base, headers, d):
        self.base, self.h, self.d = base, headers, d
        self.name = d["metadata"]["name"]
        self.color = "color" in d
        self.on = d["on"]["on"]
        self.bri = d.get("dimming", {}).get("brightness")
        self.room = None

    def set(self, on=None, bri=None, rgb=None, kelvin=None, duration=None):
        body = {}
        if on is not None:
            body["on"] = {"on": on}
        if bri is not None:
            body["dimming"] = {"brightness": max(1.0, min(100.0, bri))}
        if rgb is not None and self.color:
            body["color"] = {"xy": dict(zip("xy", rgb_to_xy(*rgb)))}
        elif kelvin is not None:
            body["color_temperature"] = {"mirek": max(153, min(500, round(1e6 / kelvin)))}
        if duration:
            body["dynamics"] = {"duration": int(duration)}
        http("PUT", f"{self.base}/resource/light/{self.d['id']}", body, self.h)


    def rename(self, name):
        http("PUT", f"{self.base}/resource/light/{self.d['id']}", {"metadata": {"name": name}}, self.h)


class IkeaLight:
    def __init__(self, base, headers, d):
        self.base, self.h, self.d = base, headers, d
        a = d["attributes"]
        self.name = a.get("customName") or d["id"]
        self.color = "colorHue" in a or "colorSaturation" in a or "color" in d.get("capabilities", {}).get("canReceive", [])
        self.on = a.get("isOn", False)
        self.bri = a.get("lightLevel")

    def set(self, on=None, bri=None, rgb=None, kelvin=None, duration=None):
        at = {}
        if on is not None:
            at["isOn"] = on
        if bri is not None:
            at["lightLevel"] = max(1, min(100, round(bri)))
        if rgb is not None and self.color:
            h, sat, _ = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
            at["colorHue"], at["colorSaturation"] = round(h * 360, 1), round(sat, 3)
        elif kelvin is not None:
            lo, hi = sorted((self.d["attributes"].get("colorTemperatureMin", 2200),
                             self.d["attributes"].get("colorTemperatureMax", 4000)))
            at["colorTemperature"] = max(lo, min(hi, kelvin))
        body = [{"attributes": at}]
        if duration:
            body[0]["transitionTime"] = int(duration)
        http("PATCH", f"{self.base}/devices/{self.d['id']}", body, self.h)


def load_config():
    if not os.path.exists(CONFIG):
        sys.exit("Not paired yet. Run: ./hue.py pair hue  and/or  ./hue.py pair ikea IP")
    return json.load(open(CONFIG))


def save_config(section, data):
    c = json.load(open(CONFIG)) if os.path.exists(CONFIG) else {}
    c[section] = data
    json.dump(c, open(CONFIG, "w"))
    os.chmod(CONFIG, 0o600)


def all_lights():
    c = load_config()
    out = []
    if "hue" in c:
        base, h = f"https://{c['hue']['ip']}/clip/v2", {"hue-application-key": c["hue"]["key"]}
        out += [HueLight(base, h, d) for d in http("GET", base + "/resource/light", headers=h)["data"]]
        room_of = {k["rid"]: r["metadata"]["name"]
                   for r in http("GET", base + "/resource/room", headers=h)["data"] for k in r["children"]}
        for l in out:
            l.room = room_of.get(l.d["owner"]["rid"])
    if "ikea" in c:
        base, h = f"https://{c['ikea']['ip']}:8443/v1", {"Authorization": "Bearer " + c["ikea"]["token"]}
        out += [IkeaLight(base, h, d) for d in http("GET", base + "/devices", headers=h)
                if d.get("type") == "light" or d.get("deviceType") == "light"]
    if not out:
        sys.exit("No lights found.")
    return out


def pick(names, room=None):
    lights = all_lights()
    if room:
        lights = [l for l in lights if l.room and room.lower() in l.room.lower()]
    if names:
        lights = [l for l in lights if any(n.lower() in l.name.lower() for n in names)]
    if not lights:
        sys.exit("No lights match.")
    return lights


# ---- color helpers -------------------------------------------------------

def rgb_to_xy(r, g, b):
    def lin(v):
        v /= 255
        return ((v + 0.055) / 1.055) ** 2.4 if v > 0.04045 else v / 12.92
    r, g, b = lin(r), lin(g), lin(b)
    X = r * 0.664511 + g * 0.154324 + b * 0.162028
    Y = r * 0.283881 + g * 0.668433 + b * 0.047685
    Z = r * 0.000088 + g * 0.072310 + b * 0.986039
    s = X + Y + Z
    return (0.3127, 0.3290) if s == 0 else (X / s, Y / s)


def hex_rgb(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def palette_at(p):
    """Smoothly interpolate the palette at position p in [0,1)."""
    seg = p % 1 * (len(PALETTE) - 1)
    i = int(seg)
    t = seg - i
    t = t * t * (3 - 2 * t)  # smoothstep
    a, b = PALETTE[i], PALETTE[i + 1]
    return tuple(a[k] + (b[k] - a[k]) * t for k in range(3))


# ---- commands ------------------------------------------------------------

def cmd_discover(_):
    try:
        found = http("GET", "https://discovery.meethue.com")
    except Exception as e:
        sys.exit(f"Discovery failed ({e}). Find the bridge IP in the Hue app and use: ./hue.py pair IP")
    for b in found:
        print(b["internalipaddress"], b.get("id", ""))
    if not found:
        print("No bridges found. Use the IP from the Hue app settings.")


def cmd_pair(a):
    if a.kind == "hue":
        ip = a.ip
        if not ip:
            found = http("GET", "https://discovery.meethue.com")
            if not found:
                sys.exit("No bridge found; pass the IP explicitly.")
            ip = found[0]["internalipaddress"]
        print(f"Press the round button on the Hue bridge ({ip}) now...")
        for _ in range(30):
            r = http("POST", f"https://{ip}/api", {"devicetype": "hue_party#mac", "generateclientkey": True})
            if "success" in r[0]:
                save_config("hue", {"ip": ip, "key": r[0]["success"]["username"]})
                print("Paired Hue.")
                return
            time.sleep(2)
        sys.exit("Timed out waiting for the button press.")
    # IKEA DIRIGERA: OAuth (PKCE) + physical button press
    if not a.ip:
        sys.exit("IKEA needs the hub IP: ./hue.py pair ikea 192.168.x.x (see your router or IKEA Home smart app)")
    base = f"https://{a.ip}:8443/v1"
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = urllib.parse.urlencode({"audience": "homesmart.local", "response_type": "code",
                                "code_challenge": challenge, "code_challenge_method": "S256"})
    code = http("GET", f"{base}/oauth/authorize?{q}")["code"]
    input("Press the ACTION button on the bottom of the IKEA hub, then hit Enter here... ")
    form = urllib.parse.urlencode({"code": code, "name": "hue_party", "grant_type": "authorization_code",
                                   "code_verifier": verifier}).encode()
    req = urllib.request.Request(f"{base}/oauth/token", data=form, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    tok = json.loads(urllib.request.urlopen(req, context=_ctx, timeout=10).read())["access_token"]
    save_config("ikea", {"ip": a.ip, "token": tok})
    print("Paired IKEA.")


def cmd_lights(_):
    for l in all_lights():
        state = "on " if l.on else "off"
        print(f'{state} {l.bri if l.bri is not None else "-":>5}%  {"color" if l.color else "white":5}  {type(l).__name__[:-5].lower():4}  {l.name}')


def cmd_rename(a):
    matches = pick([a.old])
    if len(matches) != 1:
        sys.exit("Name matches several lights: " + ", ".join(l.name for l in matches))
    if not hasattr(matches[0], "rename"):
        sys.exit("Rename IKEA lights in the IKEA Home smart app.")
    matches[0].rename(a.new)
    print(f"{matches[0].name} -> {a.new}")


def cmd_power(a, on):
    for l in pick(a.names):
        l.set(on=on)
    print("done")


def cmd_set(a):
    rgb = hex_rgb(a.color) if a.color else None
    for l in pick(a.names):
        l.set(on=True, bri=a.brightness, rgb=rgb, kelvin=a.kelvin)
    print("done")


LAVA = [
    (170, 8, 20),     # deep crimson
    (255, 45, 10),    # red-orange
    (255, 105, 10),   # molten orange
    (235, 30, 95),    # hot magenta
    (150, 15, 130),   # purple
    (170, 8, 20),
]


CYBERPUNK = [
    (255, 0, 140),    # neon magenta
    (140, 0, 255),    # electric violet
    (0, 70, 255),     # deep blue
    (0, 210, 255),    # cyan
    (255, 20, 90),    # hot pink
    (255, 0, 140),
]

SPEAKEASY = [
    (255, 95, 10),    # amber glow of backlit bottles
    (255, 60, 5),     # deep orange
    (255, 130, 30),   # whiskey gold
    (235, 45, 8),     # ember
    (255, 80, 15),
    (255, 95, 10),
]

THEMES = {"lava": LAVA, "cyberpunk": CYBERPUNK, "speakeasy": SPEAKEASY}


def lava_at(u, pal=LAVA):
    seg = u % 1 * (len(pal) - 1)
    i, t = int(seg), seg - int(seg)
    t = t * t * (3 - 2 * t)
    return tuple(pal[i][k] + (pal[i + 1][k] - pal[i][k]) * t for k in range(3))


def cmd_lava(a):
    """Each light wanders on its own slow, unsynced cycle: color blobs that swell and fade."""
    lights = pick(a.names, a.room)
    rnd = __import__("random").Random(7)
    pins = {k.lower(): hex_rgb(v) for k, v in (p.split("=") for p in a.pin)}
    # per-light: color period, brightness period (seconds), phase offsets
    cfg = [(rnd.uniform(240, 420) * a.slowness, rnd.uniform(150, 300) * a.slowness,
            rnd.random(), rnd.random()) for _ in lights]
    print(f"{a.theme} drift on {len(lights)} light(s): " + ", ".join(l.name for l in lights) + ". Ctrl-C to stop.")
    start, stop = time.time(), []
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    first = True
    while not stop:
        t = time.time() - start
        for l, (pc, pb, oc, ob) in zip(lights, cfg):
            # slow sine on color position, plus a faster wobble so it never loops predictably
            u = 0.5 + 0.5 * math.sin(2 * math.pi * (t / pc + oc)) * 0.9 + 0.1 * math.sin(2 * math.pi * (t / (pc * 0.37) + ob))
            swell = (0.5 + 0.5 * math.sin(2 * math.pi * (t / pb + ob))) ** 1.5
            bri = a.min_brightness + (a.max_brightness - a.min_brightness) * swell
            if any(d.lower() in l.name.lower() for d in a.dim):
                bri *= a.dim_scale
            try:
                l.set(on=True, bri=bri, rgb=next((c for k, c in pins.items() if k in l.name.lower()), None) or lava_at(u, THEMES[a.theme]), kelvin=2000,
                      duration=1500 if first else (a.interval + 2) * 1000)
            except Exception as e:
                print(f"  {l.name}: {e}", file=sys.stderr)
            time.sleep(0.12)
        first = False
        for _ in range(int(a.interval * 10)):
            if stop:
                break
            time.sleep(0.1)
    print("Stopped (lights left as they are).")


def cmd_night(a):
    lights = pick(a.names, a.room)
    n = len(lights)
    print(f"Night mode on {n} light(s): {a.period}-min cycle, {a.brightness}% brightness. Ctrl-C to stop.")
    start = time.time()
    stop = []
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    first = True
    while not stop:
        elapsed = time.time() - start
        if a.hours and elapsed > a.hours * 3600:
            print("Time's up, fading out.")
            for l in lights:
                l.set(on=False, duration=60000)
            return
        for i, l in enumerate(lights):
            phase = elapsed / (a.period * 60) + i / n * a.spread
            # gentle breathing of brightness so it never feels static
            breathe = 0.8 + 0.2 * math.sin(2 * math.pi * (elapsed / (a.period * 30) + i / n))
            try:
                l.set(on=True, bri=a.brightness * breathe, rgb=tuple(palette_at(phase)),
                      kelvin=2200, duration=1000 if first else a.interval * 1000)
            except Exception as e:
                print(f"  {l.name}: {e}", file=sys.stderr)
            time.sleep(0.12)  # stay under the bridges' request rate limits
        first = False
        for _ in range(int(a.interval * 10)):
            if stop:
                break
            time.sleep(0.1)
    print("Stopped (lights left as they are).")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("discover").set_defaults(f=cmd_discover)
    x = s.add_parser("pair"); x.add_argument("kind", choices=["hue", "ikea"]); x.add_argument("ip", nargs="?"); x.set_defaults(f=cmd_pair)
    s.add_parser("lights").set_defaults(f=cmd_lights)
    for name, on in (("on", True), ("off", False)):
        x = s.add_parser(name); x.add_argument("names", nargs="*")
        x.set_defaults(f=lambda a, on=on: cmd_power(a, on))
    x = s.add_parser("rename"); x.add_argument("old"); x.add_argument("new"); x.set_defaults(f=cmd_rename)
    x = s.add_parser("set"); x.add_argument("names", nargs="*")
    x.add_argument("-b", "--brightness", type=float); x.add_argument("-c", "--color", help="hex, e.g. #ff8800")
    x.add_argument("-k", "--kelvin", type=int, help="white temperature, e.g. 2200"); x.set_defaults(f=cmd_set)
    x = s.add_parser("lava"); x.add_argument("names", nargs="*")
    x.add_argument("--theme", choices=list(THEMES), default="lava")
    x.add_argument("--room"); x.add_argument("--min-brightness", type=float, default=4)
    x.add_argument("--max-brightness", type=float, default=30)
    x.add_argument("--pin", nargs="*", default=[], metavar="NAME=#HEX", help="hold these lights at a fixed color")
    x.add_argument("--dim", nargs="*", default=[], help="lights (name match) to keep dimmer")
    x.add_argument("--dim-scale", type=float, default=0.35, help="brightness multiplier for --dim lights")
    x.add_argument("--interval", type=float, default=8, help="seconds between updates (default 8)")
    x.add_argument("--slowness", type=float, default=1.0, help="2 = twice as slow"); x.set_defaults(f=cmd_lava)
    x = s.add_parser("night"); x.add_argument("names", nargs="*", help="limit to lights matching these names")
    x.add_argument("--brightness", type=float, default=12, help="peak brightness %% (default 12)")
    x.add_argument("--period", type=float, default=20, help="minutes per full color cycle (default 20)")
    x.add_argument("--interval", type=float, default=20, help="seconds between updates (default 20)")
    x.add_argument("--room"); x.add_argument("--spread", type=float, default=1.0, help="0 = all lights same color, 1 = fully offset")
    x.add_argument("--hours", type=float, help="fade out and stop after this many hours")
    x.set_defaults(f=cmd_night)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
