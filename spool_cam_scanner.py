#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Spoolman Cam Scanner - Copyright (C) 2026 Zouljiin
# https://github.com/Zouljiin/spoolman-cam-scanner
#
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this
# program. If not, see <https://www.gnu.org/licenses/>.
"""
spool_cam_scanner.py

Watches every Klipper printer's webcam for a Spoolman QR label. When a label is
seen, that spool is set as the ACTIVE spool on that printer (via Moonraker's
POST /server/spoolman/spool_id -- the same call the SET_ACTIVE_SPOOL macro makes).

Runs on ONE always-on machine (e.g. the box hosting Spoolman) and reaches out to
each printer over the LAN, so nothing has to be installed on the printers.

Recognised QR contents:
    web+spoolman:s-123        Spoolman's default label format
    SM:SPOOL=123              community barcode-scanner format
    https://<host>/spool/show/123   labels whose QR was set to a Spoolman URL
    SM:CLEAR                  special label: un-assigns the printer's current spool

When a spool is assigned, its Spoolman "location" is set to the printer's name
(or the printer's "location" setting). When a spool is cleared, its location is set
to "storage_location" if you configure one.

Feedback: every scan is confirmed in the Mainsail/Fluidd console. Optionally (see
config.example.json) it can also pop up a dialog in the web UI, run a Klipper macro on
the printer (beep / flash LEDs), and/or run a command on this machine (sound, GPIO...).

Usage:
    python3 spool_cam_scanner.py -c config.json
    python3 spool_cam_scanner.py --decode-file test.jpg     # test a photo offline
    python3 spool_cam_scanner.py -c config.json --dry-run   # log only, change nothing

Optional live log / status web page (set "web": {"enabled": true} in config.json):
    http://<this-machine>:8090/            page (embeddable in dashboards)
    http://<this-machine>:8090/api/status  JSON status of every printer
    http://<this-machine>:8090/api/summary flat JSON for dashboard widgets
    http://<this-machine>:8090/api/log     JSON log lines
"""
import argparse
import collections
import hmac
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import cv2
import numpy as np
import requests

try:  # optional, noticeably better at blurry / small codes
    from pyzbar import pyzbar
except Exception:  # pragma: no cover
    pyzbar = None

__version__ = "0.1.0"

log = logging.getLogger("spoolscan")

SPOOL_PATTERNS = [
    re.compile(r"web\+spoolman:s-(\d+)", re.I),
    re.compile(r"SM:SPOOL=(\d+)", re.I),
    re.compile(r"/spool/show/(\d+)", re.I),
]


CLEAR = "clear"
CLEAR_PATTERN = re.compile(r"SM:CLEAR|web\+spoolman:clear", re.I)


def parse_spool_id(text):
    """Return a spool id (int), CLEAR, or None."""
    if CLEAR_PATTERN.search(text):
        return CLEAR
    for pat in SPOOL_PATTERNS:
        m = pat.search(text)
        if m:
            return int(m.group(1))
    return None


def spool_desc(info):
    """'Vendor Name Material' from a Spoolman spool record."""
    fil = (info or {}).get("filament", {}) or {}
    return " ".join(x for x in (
        (fil.get("vendor") or {}).get("name"), fil.get("name"), fil.get("material")) if x)


class LogBuffer(logging.Handler):
    """Keeps the most recent log lines in memory for the web page."""

    def __init__(self, maxlen=1000):
        super().__init__()
        self.lines = collections.deque(maxlen=maxlen)
        self.next_id = 1

    def emit(self, record):  # called with self.lock held
        msg = record.getMessage()
        m = re.match(r"\[(.+?)\] (.*)", msg, re.S)
        printer, text = (m.group(1), m.group(2)) if m else ("", msg)
        self.lines.append({"id": self.next_id, "t": round(record.created, 3),
                           "level": record.levelname, "printer": printer, "msg": text})
        self.next_id += 1

    def since(self, after=0, limit=500):
        self.acquire()
        try:
            data = list(self.lines)
            last = self.next_id - 1
        finally:
            self.release()
        return [l for l in data if l["id"] > after][-limit:], last


class Decoder:
    """QR decoding with a few fallbacks. One instance per thread."""

    def __init__(self):
        self.aruco = cv2.QRCodeDetectorAruco() if hasattr(cv2, "QRCodeDetectorAruco") else None
        self.plain = cv2.QRCodeDetector()

    def _variants(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        yield gray
        if gray.shape[1] < 1400:
            yield cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        yield cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)

    def decode(self, frame):
        """Return a list of decoded strings found in the frame."""
        for img in self._variants(frame):
            found = []
            if pyzbar is not None:
                found += [c.data.decode("utf-8", "ignore") for c in pyzbar.decode(img)
                          if c.type == "QRCODE"]
            for det in (self.aruco, self.plain):
                if det is None or found:
                    continue
                try:
                    text, _, _ = det.detectAndDecode(img)
                except cv2.error:
                    continue
                if text:
                    found.append(text)
            if found:
                return found
        return []


class Printer(threading.Thread):
    def __init__(self, cfg, settings, registry, dry_run=False):
        super().__init__(daemon=True, name=cfg["name"])
        self.name_ = cfg["name"]
        self.cfg = cfg
        self.s = settings
        self.registry = registry
        self.dry_run = dry_run
        self.base = cfg["moonraker"].rstrip("/")
        self.http = requests.Session()
        if cfg.get("api_key"):
            self.http.headers["X-Api-Key"] = cfg["api_key"]
        self.snapshot_url = cfg.get("snapshot_url")
        self.location = cfg.get("location", cfg["name"])
        gfb = settings.get("feedback") or {}
        pfb = cfg.get("feedback") or {}
        self.fb = {**gfb, **pfb, "gcode": {**(gfb.get("gcode") or {}), **(pfb.get("gcode") or {})}}
        self.decoder = Decoder()
        self.last_seen = None
        self.last_seen_t = 0.0
        self.last_err_log = 0.0
        self.prev_small = None      # previous downscaled frame (motion detection)
        self.active_until = 0.0     # keep decoding until this time
        self.busy = False           # printer is printing -> poll slowly
        self.state_t = 0.0
        self.stop_event = threading.Event()
        self.quiet = False          # True for throw-away lookup objects: no INFO log lines
        self.camera = "starting"    # starting | ok | error
        self.last_error = ""
        self.print_state_str = ""
        self.active = None          # last known active spool id
        self.desc_cache = {}        # spool id -> "Vendor Name Material"
        self.last_event = None

    # ---------------- Moonraker helpers ----------------
    def moon(self, method, path, **kw):
        r = self.http.request(method, self.base + path, timeout=6, **kw)
        r.raise_for_status()
        return r.json().get("result", {})

    def print_state(self):
        try:
            res = self.moon("GET", "/printer/objects/query", params={"print_stats": ""})
            return res["status"]["print_stats"]["state"]
        except Exception:
            return ""  # Klipper not ready etc. -- Moonraker can still set the spool

    def active_spool(self):
        return self.moon("GET", "/server/spoolman/spool_id").get("spool_id")

    def set_active(self, sid):
        return self.moon("POST", "/server/spoolman/spool_id", json={"spool_id": sid} if sid is not None else {})

    def spool_info(self, sid):
        """dict = found, False = definitely not in Spoolman, {} = couldn't check."""
        try:
            res = self.moon("POST", "/server/spoolman/proxy", json={
                "request_method": "GET", "path": f"/v1/spool/{sid}", "use_v2_response": True})
        except Exception as e:
            log.warning("[%s] could not look up spool %s: %s", self.name_, sid, e)
            return {}
        err = res.get("error")
        if err:
            return False if err.get("status_code") == 404 else {}
        return res.get("response") or {}

    def set_location(self, sid, location):
        """Update the spool's Location field in Spoolman (via Moonraker's proxy)."""
        try:
            res = self.moon("POST", "/server/spoolman/proxy", json={
                "request_method": "PATCH", "path": f"/v1/spool/{sid}",
                "body": {"location": location}, "use_v2_response": True})
            if res.get("error"):
                log.warning("[%s] could not set location on spool %s: %s",
                            self.name_, sid, res["error"])
            else:
                log.info("[%s] spool %s location -> %r", self.name_, sid, location)
        except Exception as e:
            log.warning("[%s] could not set location on spool %s: %s", self.name_, sid, e)

    FALLBACK = {"already_active": "assigned", "nothing_to_clear": "cleared", "removed": "cleared"}

    def feedback(self, event, msg, sid=None, desc="", state=""):
        """Confirm a scan. Events: assigned, already_active, cleared, nothing_to_clear,
        not_found, ignored (printer busy)."""
        log.debug("[%s] feedback %s: %s", self.name_, event, msg)
        self.last_event = {"time": round(time.time(), 3), "event": event, "message": msg}
        if self.dry_run:
            return
        if state != "printing":  # never inject anything into a running job
            self._gcode(f'RESPOND MSG="{self._clean(msg)}"')
            macros = self.fb.get("gcode") or {}
            macro = macros.get(event) or macros.get(self.FALLBACK.get(event, ""))
            if macro:
                self._gcode(f"{macro} SPOOL={sid}" if sid else macro)
            if self.fb.get("popup") and event != "ignored":
                self._popup(msg)
        cmd = self.fb.get("command")  # runs on THIS machine; fine even while printing
        if cmd:
            env = dict(os.environ, SPOOL_EVENT=event, SPOOL_PRINTER=self.name_,
                       SPOOL_ID=str(sid or ""), SPOOL_DESC=desc, SPOOL_MESSAGE=msg)

            def _run():
                try:
                    subprocess.run(shlex.split(cmd), env=env, timeout=20,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception as e:
                    log.warning("[%s] feedback command failed: %s", self.name_, e)
            threading.Thread(target=_run, daemon=True).start()

    @staticmethod
    def _clean(text):
        return re.sub(r'["|\n\r]', "'", text)

    def _gcode(self, script):
        try:
            self.moon("POST", "/printer/gcode/script", json={"script": script})
        except Exception as e:
            log.debug("[%s] gcode %r failed: %s", self.name_, script, e)

    def _popup(self, msg):
        """Dialog in Mainsail/Fluidd/KlipperScreen using Klipper's action:prompt protocol."""
        end = 'RESPOND TYPE=command MSG="action:prompt_end"'
        self._gcode("\n".join([
            'RESPOND TYPE=command MSG="action:prompt_begin Spool scanner"',
            f'RESPOND TYPE=command MSG="action:prompt_text {self._clean(msg)}"',
            'RESPOND TYPE=command MSG="action:prompt_footer_button OK|RESPOND TYPE=command '
            'MSG=action:prompt_end|primary"',
            'RESPOND TYPE=command MSG="action:prompt_show"']))
        secs = self.fb.get("popup_seconds", 4)
        self._popup_gen = getattr(self, "_popup_gen", 0) + 1
        gen = self._popup_gen
        if secs:  # auto-dismiss, unless a newer popup has replaced this one
            t = threading.Timer(secs, lambda: gen == self._popup_gen and self._gcode(end))
            t.daemon = True
            t.start()

    def spoolman_locations(self):
        """Location names currently in use in Spoolman, read through this printer's Moonraker."""
        def get(path):
            res = self.moon("POST", "/server/spoolman/proxy", json={
                "request_method": "GET", "path": path, "use_v2_response": True})
            if res.get("error"):
                raise RuntimeError(res["error"])
            return res.get("response")

        names = []
        try:
            for item in get("/v1/location") or []:
                names.append(item.get("name") if isinstance(item, dict) else item)
        except Exception:
            pass
        if not names:  # fall back to the distinct locations found on the spools themselves
            names = [sp.get("location") for sp in (get("/v1/spool") or []) if isinstance(sp, dict)]
        return sorted({n.strip() for n in names if isinstance(n, str) and n.strip()}, key=str.lower)

    def release(self, sid, to=None):
        """Called on the *other* printers when a spool is claimed elsewhere."""
        try:
            if self.active_spool() != sid:
                return
            if self.print_state() == "printing":
                log.warning("[%s] still lists spool %s but is printing; left alone", self.name_, sid)
                return
            log.info("[%s] releasing spool %s (moved to %s)", self.name_, sid, to or "another printer")
            if not self.dry_run:
                self.set_active(None)
                self.active = None
                self.feedback("removed", f"Spool #{sid} removed (moved to {to or 'another printer'})", sid)
        except Exception as e:
            log.warning("[%s] release failed: %s", self.name_, e)

    # ---------------- camera ----------------
    def discover_snapshot(self):
        cams = self.moon("GET", "/server/webcams/list").get("webcams", [])
        cams = [c for c in cams if c.get("enabled", True) and c.get("snapshot_url")]
        want = self.cfg.get("webcam")
        if want:
            cams = [c for c in cams if c.get("name") == want]
        if not cams:
            raise RuntimeError("no enabled webcam with a snapshot_url in Moonraker "
                               "(set 'snapshot_url' in the config)")
        self.detected_webcam = cams[0].get("name", "")
        url = cams[0]["snapshot_url"]
        host = urlparse(self.base).hostname
        if not urlparse(url).scheme:
            # relative -> served by nginx on port 80 of the printer, not the Moonraker port
            url = urljoin(f"http://{host}/", url)
        else:
            p = urlparse(url)
            if p.hostname in ("localhost", "127.0.0.1"):
                url = p._replace(netloc=f"{host}:{p.port}" if p.port else host).geturl()
        (log.debug if self.quiet else log.info)("[%s] using camera snapshot %s", self.name_, url)
        return url

    def grab(self):
        r = self.http.get(self.snapshot_url, timeout=6)
        r.raise_for_status()
        img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("snapshot was not a decodable image")
        return img

    def refresh_status(self):
        """Cheap poll (every ~5 s) of print state and active spool, for status + slow-down."""
        self.print_state_str = self.print_state()
        try:
            self.active = self.active_spool()
            if self.active is not None and self.active not in self.desc_cache:
                info = self.spool_info(self.active)
                self.desc_cache[self.active] = spool_desc(info) if info else ""
        except Exception:
            pass

    def status(self):
        return {
            "name": self.name_, "moonraker": self.base, "camera": self.camera,
            "error": self.last_error, "print_state": self.print_state_str,
            "spool": self.active, "spool_desc": self.desc_cache.get(self.active, ""),
            "location": self.location, "color": self.cfg.get("color", ""), "last_event": self.last_event,
        }

    def moving(self, frame):
        """Cheap motion check on a tiny greyscale copy of the frame."""
        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90),
                           interpolation=cv2.INTER_AREA)
        moved = (self.prev_small is None or
                 float(np.mean(cv2.absdiff(small, self.prev_small))) > self.s["motion_threshold"])
        self.prev_small = small
        return moved

    # ---------------- main logic ----------------
    def handle(self, sid):
        now = time.time()
        held = sid == self.last_seen and now - self.last_seen_t < self.s["cooldown"]
        self.last_seen, self.last_seen_t = sid, now
        if held:
            return  # same spool still in front of the camera

        state = self.print_state()
        if state == "printing" and self.s["skip_while_printing"]:
            log.info("[%s] saw spool %s but printer is printing -- ignored", self.name_, sid)
            self.feedback("ignored", "Printer is busy - spool scan ignored",
                          None if sid == CLEAR else sid, state=state)
            return
        if sid == CLEAR:
            return self.clear_spool(state)
        cur = self.active_spool()
        if cur == sid:
            log.info("[%s] spool %s is already active", self.name_, sid)
            self.feedback("already_active", f"Spool #{sid} is already active", sid, state=state)
            return

        info = self.spool_info(sid)
        if info is False:
            log.warning("[%s] spool %s is not in Spoolman", self.name_, sid)
            self.feedback("not_found", f"Spool #{sid} not found in Spoolman", sid, state=state)
            return

        desc = spool_desc(info)
        self.desc_cache[sid] = desc
        log.info("[%s] setting active spool -> #%s %s", self.name_, sid, desc)
        if self.dry_run:
            return
        self.set_active(sid)
        self.active = sid
        if self.s["set_location"]:
            self.set_location(sid, self.location)
        msg = f"Active spool set to #{sid} {desc}".strip()
        if cur is not None:  # a different spool was loaded before: it has been taken off
            if self.s["storage_location"]:
                self.set_location(cur, self.s["storage_location"])
            msg += f" - spool #{cur} removed"
        self.feedback("assigned", msg, sid, desc, state)
        if self.s["exclusive"]:
            for other in list(self.registry):
                if other is not self:
                    other.release(sid, self.name_)

    def clear_spool(self, state):
        cur = self.active_spool()
        if cur is None:
            log.info("[%s] CLEAR label seen, but no spool is active", self.name_)
            self.feedback("nothing_to_clear", "No active spool to clear", None, state=state)
            return
        log.info("[%s] clearing active spool (was #%s)", self.name_, cur)
        if self.dry_run:
            return
        self.set_active(None)
        self.active = None
        if self.s["storage_location"]:
            self.set_location(cur, self.s["storage_location"])
        self.feedback("cleared", f"Spool #{cur} removed (clear label scanned)", cur, state=state)

    def run(self):
        log.info("[%s] watching %s", self.name_, self.base)
        while not self.stop_event.is_set():
            try:
                if not self.snapshot_url:
                    self.snapshot_url = self.discover_snapshot()
                frame = self.grab()
                self.camera, self.last_error = "ok", ""
                now = time.time()
                # Full QR decoding is the expensive part, so only do it while something is
                # moving in front of the camera (plus a short hold for a spool held still).
                if not self.s["motion_gate"] or self.moving(frame):
                    self.active_until = now + self.s["motion_hold"]
                if now < self.active_until:
                    for text in self.decoder.decode(frame):
                        sid = parse_spool_id(text)
                        if sid is not None:
                            self.handle(sid)
                            break
                        log.debug("[%s] QR with unrelated content: %r", self.name_, text)
                if now - self.state_t > 5:  # while printing, look much less often
                    self.refresh_status()
                    self.busy = self.s["skip_while_printing"] and self.print_state_str == "printing"
                    self.state_t = now
                self.stop_event.wait(self.s["busy_poll_interval"] if self.busy else self.s["poll_interval"])
            except Exception as e:
                self.camera, self.last_error = "error", f"{type(e).__name__}: {e}"[:300]
                if time.time() - self.last_err_log > 60:
                    log.warning("[%s] %s: %s (retrying)", self.name_, type(e).__name__, e)
                    self.last_err_log = time.time()
                self.stop_event.wait(10)


PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spoolman Cam Scanner</title>
<style>
:root{--bg:#0f1216;--panel:#171b21;--text:#d7dde5;--dim:#8a94a3;--line:#252b34;--ok:#3fb950;--err:#f85149;--warn:#d29922;--idle:#6e7681;--accent:#58a6ff}
:root[data-theme=light]{--bg:#f6f8fa;--panel:#ffffff;--text:#1f2328;--dim:#59636e;--line:#d1d9e0;--ok:#1a7f37;--err:#cf222e;--warn:#9a6700;--idle:#8c959f;--accent:#0969da}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--text);font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:12px;padding:10px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:15px;margin:0;font-weight:600}
.spacer{flex:1}
.conn{font-size:12px;color:var(--dim)} .conn i{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--idle);margin-right:5px}
.conn.up i{background:var(--ok)} .conn.down i{background:var(--err)}
button,select{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:4px 9px;font:inherit;font-size:12px;cursor:pointer}
button:hover,select:hover{border-color:var(--accent)}
#cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;padding:10px 14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:9px 11px;min-width:0}
.card .top{display:flex;align-items:center;gap:7px;font-weight:600}
.card .nm{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card .vis{margin-left:auto;flex:none;padding:2px 10px;font-size:11px}
.card.off .nm{opacity:.55} .card.off .vis{color:var(--dim)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--idle);flex:none}
.dot.ok{background:var(--ok)} .dot.error{background:var(--err)}
.card .row{color:var(--dim);font-size:12px;margin-top:3px;white-space:normal;overflow-wrap:anywhere}
body:not(.scroll-text) .card .row.err{display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
body.scroll-text .card .row{white-space:nowrap;overflow:hidden;overflow-wrap:normal}
body.scroll-text .card .tx{display:inline-block}
body.scroll-text .card .tx.mq{animation:mq var(--dur,8s) ease-in-out infinite alternate}
@keyframes mq{0%,12%{transform:translateX(0)}88%,100%{transform:translateX(var(--shift,0px))}}
@media (prefers-reduced-motion:reduce){body.scroll-text .card .tx.mq{animation:none}}
.card .stat.printing{color:var(--warn)}
.card .spool{color:var(--text);font-size:13px}
#logwrap{flex:1;min-height:0;padding:0 14px 12px;display:flex;flex-direction:column}
#log{flex:1;min-height:0;overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 0;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.line{display:flex;gap:9px;padding:1px 10px;align-items:baseline}
.line:hover{background:rgba(127,127,127,.08)}
.t{color:var(--dim);flex:none}
.p{flex:none;font-weight:600}
.m{white-space:pre-wrap;word-break:break-word;min-width:0}
.WARNING .m{color:var(--warn)} .ERROR .m,.CRITICAL .m{color:var(--err)} .DEBUG .m{color:var(--dim)}
.empty{padding:14px;color:var(--dim)}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;align-items:center;justify-content:center;padding:14px;z-index:10}
.modal-bg.open{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:10px;width:min(560px,100%);max-height:100%;overflow:auto;padding:14px 16px}
.modal h2{margin:0 0 8px;font-size:15px}
.modal p{margin:6px 0;color:var(--dim);font-size:12px}
.modal label{display:block;font-size:12px;color:var(--dim);margin:9px 0 3px}
.modal input{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
.modal .crow{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.modal input[type=color]{width:40px;height:28px;padding:1px;flex:none}
.modal .sw{width:22px;height:22px;padding:0;border-radius:50%;border:2px solid var(--line);flex:none}
.modal .sw:hover{border-color:var(--text)}
.modal .auto.on{border-color:var(--accent);color:var(--accent)}
.modal label.chk{display:flex;align-items:center;gap:8px;color:var(--text);font-size:13px;margin:6px 0 2px;cursor:pointer}
.modal input[type=checkbox]{width:auto;margin:0}
.modal .hint{font-size:11px;color:var(--dim);margin-top:2px}
.modal .btns{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.prow{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--line)}
.prow .pn{font-weight:600} .prow .pu{color:var(--dim);font-size:12px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.res{margin-top:10px;font-size:12px;padding:8px;border-radius:6px;border:1px solid var(--line);white-space:pre-wrap;word-break:break-word}
.res.good{border-color:var(--ok)} .res.bad{border-color:var(--err)}
button.danger{color:var(--err)}
body.log-only header,body.log-only #cards{display:none}
body.status-only #logwrap{display:none}
body.log-only #logwrap{padding-top:0} body.log-only #log{border-radius:0;border:0}
</style>
</head>
<body>
<header>
  <h1>Spoolman Cam Scanner</h1>
  <span class="conn" id="conn"><i></i><span id="conntxt">connecting…</span></span>
  <span class="spacer"></span>
  <select id="lfilter" title="Minimum level"><option value="">All levels</option><option value="nowarn">Hide warnings</option><option value="WARNING">Warnings + errors only</option></select>
  <button id="textmode" title="How long lines on the printer cards are shown">Long text: Wrap</button>
  <button id="manage" style="display:none">Manage printers</button>
  <button id="pause">Pause</button>
  <button id="clear">Clear view</button>
</header>
<div class="modal-bg" id="mbg"><div class="modal" id="modal"></div></div>
<div id="cards"></div>
<div id="logwrap"><div id="log"><div class="empty">Waiting for log lines…</div></div></div>
<script>
(function(){
  var q = new URLSearchParams(location.search);
  var theme = q.get('theme');
  if (theme === 'light' || theme === 'dark') document.documentElement.setAttribute('data-theme', theme);
  else if (window.matchMedia && !window.matchMedia('(prefers-color-scheme: dark)').matches) document.documentElement.setAttribute('data-theme', 'light');
  var view = q.get('view');
  if (view === 'log') document.body.classList.add('log-only');
  if (view === 'status') document.body.classList.add('status-only');
  var maxLines = parseInt(q.get('lines') || '300', 10);
  // Settings are remembered in this browser (localStorage); URL options override for that visit.
  function store(k, v){ try { if (v === undefined) return localStorage.getItem('sc.' + k); localStorage.setItem('sc.' + k, v); } catch (e) {} return null; }
  var after = 0, paused = false, only = q.get('printer') || '';
  var lfilter = q.get('level') || store('level') || '';
  var textMode = q.get('text') || store('text') || 'wrap';
  var hidden = {};
  (q.has('hide') ? q.get('hide').split(',') : (function(){ try { return JSON.parse(store('hidden') || '[]'); } catch (e) { return []; } })())
    .forEach(function(n){ if (n) hidden[n] = true; });
  function saveHidden(){ store('hidden', JSON.stringify(Object.keys(hidden))); }
  var logEl = document.getElementById('log'), cardsEl = document.getElementById('cards');
  var connEl = document.getElementById('conn'), connTxt = document.getElementById('conntxt');
  document.getElementById('lfilter').value = lfilter;
  var buffer = [];

  function el(tag, cls, text){ var e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
  // Printer colors: the one chosen in Manage printers, else a distinct automatic one (spread around the color wheel by position)
  var PRESETS = ['#e69f00', '#4aa3ff', '#2fc78a', '#ff6b6b', '#c58bff', '#f2d64b', '#3fd0d0', '#f472b6'];
  function isLight(){ return document.documentElement.getAttribute('data-theme') === 'light'; }
  function hslHex(h, s, l){
    s /= 100; l /= 100; var a = s * Math.min(l, 1 - l);
    function f(n){ var k = (n + h / 30) % 12, c = l - a * Math.max(-1, Math.min(k - 3, Math.min(9 - k, 1))); return ('0' + Math.round(255 * c).toString(16)).slice(-2); }
    return '#' + f(0) + f(8) + f(4);
  }
  function autoColor(i){ return hslHex((i * 137.508 + 25) % 360, 70, isLight() ? 38 : 62); }
  var colorMap = {}, printerOrder = [];
  function colorOf(name){
    if (colorMap[name]) return colorMap[name];
    var h = 0; for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
    return hslHex(h, 65, isLight() ? 38 : 62);
  }
  function fmt(t){ var d = new Date(t * 1000); return d.toLocaleTimeString([], {hour12:false}); }
  function ago(t){ var s = Math.max(0, Math.round(Date.now()/1000 - t)); if (s < 60) return s + 's ago'; if (s < 3600) return Math.floor(s/60) + 'm ago'; if (s < 86400) return Math.floor(s/3600) + 'h ago'; return Math.floor(s/86400) + 'd ago'; }
  function setConn(up){ connEl.className = 'conn ' + (up ? 'up' : 'down'); connTxt.textContent = up ? 'live' : 'disconnected'; }

  function passes(l){
    if (l.printer && hidden[l.printer]) return false;   // switched off with the card's Hide button
    if (only && l.printer !== only) return false;
    if (lfilter === 'nowarn' && l.level === 'WARNING') return false;
    if (lfilter === 'WARNING' && l.level !== 'WARNING' && l.level !== 'ERROR' && l.level !== 'CRITICAL') return false;
    return true;
  }
  function lineNode(l){
    var d = el('div', 'line ' + l.level);
    d.appendChild(el('span', 't', fmt(l.t)));
    if (l.printer){ var p = el('span', 'p', l.printer); p.style.color = colorOf(l.printer); d.appendChild(p); }
    d.appendChild(el('span', 'm', l.msg));
    return d;
  }
  function render(){
    var atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    logEl.textContent = '';
    var shown = buffer.filter(passes);
    if (!shown.length) logEl.appendChild(el('div', 'empty', 'No log lines yet.'));
    shown.forEach(function(l){ logEl.appendChild(lineNode(l)); });
    if (atBottom) logEl.scrollTop = logEl.scrollHeight;
  }
  function pollLog(){
    return fetch('api/log?after=' + after).then(function(r){ return r.json(); }).then(function(d){
      setConn(true);
      if (d.last_id < after){ after = 0; buffer = []; render(); return; }  // server restarted
      if (paused) return;  // do not advance: the lines are fetched again on resume
      after = d.last_id;
      if (!d.lines.length) return;
      d.lines.forEach(function(l){ buffer.push(l); });
      if (buffer.length > maxLines) buffer = buffer.slice(-maxLines);
      render();
    }).catch(function(){ setConn(false); });
  }
  var cardsMap = {};
  function mkRow(cls){ var r = el('div', 'row ' + cls), t = el('span', 'tx'); r.appendChild(t); return {row: r, tx: t, shift: 0}; }
  function setTx(o, text){ if (o.tx.textContent !== text) o.tx.textContent = text; }
  function paintVis(name){
    var c = cardsMap[name], off = !!hidden[name];
    c.vis.textContent = off ? 'Show' : 'Hide';
    c.vis.title = off ? "Show this printer's lines in the log again" : "Hide this printer's lines from the log";
    c.root.classList.toggle('off', off);
  }
  function buildCard(name){
    var root = el('div', 'card'), top = el('div', 'top');
    var c = {root: root, dot: el('span', 'dot'), nm: el('span', 'nm', name), vis: el('button', 'vis'),
             stat: mkRow('stat'), spool: mkRow('spool'), ev: mkRow('ev'), err: mkRow('err')};
    c.nm.title = name;
    c.vis.addEventListener('click', function(){
      if (hidden[name]) delete hidden[name]; else hidden[name] = true;
      saveHidden(); paintVis(name); render();
    });
    top.appendChild(c.dot); top.appendChild(c.nm); top.appendChild(c.vis); root.appendChild(top);
    [c.stat, c.spool, c.ev, c.err].forEach(function(o){ root.appendChild(o.row); });
    cardsMap[name] = c; return c;
  }
  function updateCard(p){
    var c = cardsMap[p.name] || buildCard(p.name);
    c.dot.className = 'dot ' + p.camera;
    c.dot.title = 'camera: ' + p.camera + (p.error ? ' — ' + p.error : '');
    setTx(c.stat, 'Printer Status: ' + (p.print_state || 'unknown'));
    c.stat.row.classList.toggle('printing', p.print_state === 'printing');
    setTx(c.spool, p.spool ? ('#' + p.spool + (p.spool_desc ? ' ' + p.spool_desc : '')) : 'no spool assigned');
    var camErr = p.camera === 'error', showEv = !camErr && !!p.last_event;
    c.err.row.style.display = camErr ? '' : 'none';
    if (camErr){ setTx(c.err, 'camera: ' + (p.error || 'error')); c.err.tx.title = p.error || ''; }
    c.ev.row.style.display = showEv ? '' : 'none';
    if (showEv) setTx(c.ev, p.last_event.message + ' · ' + ago(p.last_event.time));
    paintVis(p.name);
    return c;
  }
  // "Scroll" mode: a line that is wider than its card slides back and forth
  function layoutMarquee(){
    Object.keys(cardsMap).forEach(function(n){
      ['stat', 'spool', 'ev', 'err'].forEach(function(k){
        var o = cardsMap[n][k];
        if (textMode !== 'scroll' || o.row.style.display === 'none'){ o.tx.classList.remove('mq'); return; }
        var over = o.tx.offsetWidth - o.row.clientWidth;
        if (over > 2){
          if (Math.abs(over - o.shift) > 3){
            o.shift = over;
            o.tx.style.setProperty('--shift', (-over) + 'px');
            o.tx.style.setProperty('--dur', Math.max(5, over / 22).toFixed(1) + 's');
          }
          o.tx.classList.add('mq');
        } else { o.shift = 0; o.tx.classList.remove('mq'); }
      });
    });
  }
  function applyTextMode(){
    document.body.classList.toggle('scroll-text', textMode === 'scroll');
    document.getElementById('textmode').textContent = 'Long text: ' + (textMode === 'scroll' ? 'Scroll' : 'Wrap');
    layoutMarquee();
  }
  function pollStatus(){
    return fetch('api/status').then(function(r){ return r.json(); }).then(function(d){
      manageBtn.style.display = d.editable ? '' : 'none'; tokenRequired = !!d.token_required;
      var names = d.printers.map(function(p){ return p.name; });
      var nm = {}; d.printers.forEach(function(p, i){ nm[p.name] = p.color || autoColor(i); });
      var colorsChanged = JSON.stringify(nm) !== JSON.stringify(colorMap);
      colorMap = nm; printerOrder = names;
      Object.keys(cardsMap).forEach(function(n){       // printers that were removed
        if (names.indexOf(n) < 0){ cardsEl.removeChild(cardsMap[n].root); delete cardsMap[n]; }
      });
      d.printers.forEach(function(p, i){
        var c = updateCard(p);
        if (cardsEl.children[i] !== c.root) cardsEl.insertBefore(c.root, cardsEl.children[i] || null);
      });
      names.forEach(function(n){ cardsMap[n].nm.style.color = colorMap[n]; });
      if (colorsChanged) render();
      layoutMarquee();
    }).catch(function(){});
  }

  // ---------- manage printers (only when the server has editing enabled) ----------
  var token = '', tokenRequired = false; try { token = sessionStorage.getItem('scanner_token') || ''; } catch (e) {}
  var manageBtn = document.getElementById('manage'), mbg = document.getElementById('mbg'), modal = document.getElementById('modal');

  function api(method, path, body){
    var h = {'Content-Type': 'application/json', 'X-Scanner-Client': 'web'}; if (token) h['Authorization'] = 'Bearer ' + token;
    return fetch(path, {method: method, headers: h, body: body ? JSON.stringify(body) : undefined}).then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(j){ return {status: r.status, data: j}; });
    });
  }
  function openModal(){ mbg.classList.add('open'); }
  function closeModal(){ mbg.classList.remove('open'); modal.textContent = ''; }
  function btn(label, fn, cls){ var b = el('button', cls || '', label); b.addEventListener('click', fn); return b; }
  function heading(t){ modal.textContent = ''; modal.appendChild(el('h2', '', t)); }

  function showToken(msg){
    heading('Manage printers'); openModal();
    modal.appendChild(el('p', '', msg || 'Enter the edit token (web.edit_token in config.json).'));
    var inp = el('input'); inp.type = 'password'; inp.id = 'tokeninput'; modal.appendChild(inp);
    var go = function(){ token = inp.value; try { sessionStorage.setItem('scanner_token', token); } catch (e) {} showList(); };
    inp.addEventListener('keydown', function(e){ if (e.key === 'Enter') go(); });
    var b = el('div', 'btns'); b.appendChild(btn('Unlock', go)); b.appendChild(btn('Cancel', closeModal)); modal.appendChild(b);
    inp.focus();
  }

  function showList(){
    if (tokenRequired && !token) return showToken();
    api('GET', 'api/config').then(function(r){
      if (r.status === 401){ token = ''; try { sessionStorage.removeItem('scanner_token'); } catch (e) {} return showToken('Wrong token, try again.'); }
      if (r.status !== 200){ heading('Manage printers'); openModal(); modal.appendChild(el('p', '', r.data.error || 'Error')); modal.appendChild(btn('Close', closeModal)); return; }
      heading('Manage printers'); openModal();
      var list = el('div', 'plist');
      r.data.printers.forEach(function(p){
        var row = el('div', 'prow');
        var pn = el('span', 'pn', p.name); pn.style.color = p.color || colorOf(p.name);
        row.appendChild(pn); row.appendChild(el('span', 'pu', p.moonraker));
        row.appendChild(btn('Edit', function(){ showForm(p); }));
        row.appendChild(btn('Remove', function(){
          if (!window.confirm('Remove ' + p.name + '?')) return;
          api('DELETE', 'api/printers/' + encodeURIComponent(p.name)).then(function(){ showList(); pollStatus(); });
        }, 'danger'));
        list.appendChild(row);
      });
      if (!r.data.printers.length) list.appendChild(el('p', '', 'No printers yet.'));
      modal.appendChild(list);
      var b = el('div', 'btns'); b.appendChild(btn('Add printer', function(){ showForm(null); })); b.appendChild(btn('Close', closeModal)); modal.appendChild(b);
    });
  }

  function showForm(existing){
    heading(existing ? 'Edit ' + existing.name : 'Add printer'); openModal();
    var f = {};
    function add(key, label, hint, type, ph){
      modal.appendChild(el('label', '', label));
      var i = el('input'); i.type = type || 'text'; i.value = existing && existing[key] ? existing[key] : ''; if (ph) i.placeholder = ph;
      f[key] = i; modal.appendChild(i); if (hint) modal.appendChild(el('div', 'hint', hint));
    }
    add('name', 'Name', 'Shown in messages and used as the spool location.');
    add('moonraker', 'Moonraker address', 'e.g. 192.168.1.50 (port 7125 is assumed). Then click Auto detect to fill in the rest.');
    add('snapshot_url', 'Camera snapshot URL (optional)', 'Leave blank to auto-detect from Moonraker.');
    add('webcam', 'Webcam name (optional)', 'Only needed if the printer has several webcams.');
    // Spoolman location: pick from the locations Spoolman already has, or type a new one
    modal.appendChild(el('label', '', 'Spoolman location (optional)'));
    var lsel = el('select'); lsel.style.width = '100%';
    var ltxt = el('input'); ltxt.placeholder = 'New location name'; ltxt.style.display = 'none'; ltxt.style.marginTop = '6px';
    var lhint = el('div', 'hint', 'Loading Spoolman locations…');
    var wanted = existing && existing.location ? existing.location : '', locLoaded = false;
    function fillLocations(names){
      lsel.textContent = '';
      var opts = [['', 'Default (printer name)']];
      names.forEach(function(n){ opts.push([n, n]); });
      if (wanted && names.indexOf(wanted) < 0) opts.push([wanted, wanted]);
      opts.push(['__other__', 'Other (type a new name)…']);
      opts.forEach(function(o){ var op = el('option', '', o[1]); op.value = o[0]; lsel.appendChild(op); });
      lsel.value = wanted;
    }
    lsel.addEventListener('change', function(){
      ltxt.style.display = lsel.value === '__other__' ? '' : 'none';
      if (lsel.value === '__other__') ltxt.focus();
    });
    fillLocations([]);
    f.location = { get value(){ return lsel.value === '__other__' ? ltxt.value : lsel.value; } };
    modal.appendChild(lsel); modal.appendChild(ltxt); modal.appendChild(lhint);
    function loadLocations(){
      var q = f.moonraker.value.trim();
      api('GET', 'api/locations' + (q ? '?moonraker=' + encodeURIComponent(q) : '')).then(function(r){
        if (r.status === 401){ token = ''; showToken('Wrong token, try again.'); return; }
        if (r.status !== 200){ lhint.textContent = (r.data && r.data.error) || 'Could not load Spoolman locations.'; return; }
        wanted = f.location.value || wanted;
        fillLocations(r.data.locations); locLoaded = true;
        lhint.textContent = r.data.locations.length + ' location(s) from Spoolman. "Default" uses the printer name.';
      });
    }
    // Log color: a preset, any color, or Auto (a distinct color is chosen for you)
    modal.appendChild(el('label', '', 'Log color'));
    var crow = el('div', 'crow'), cinp = el('input'), autoBtn = el('button', 'auto', 'Auto');
    var chint = el('div', 'hint', ''), cauto = !(existing && existing.color);
    var autoIdx = existing ? Math.max(0, printerOrder.indexOf(existing.name)) : printerOrder.length;
    cinp.type = 'color'; cinp.value = existing && existing.color ? existing.color : autoColor(autoIdx);
    function paintColor(){
      autoBtn.classList.toggle('on', cauto);
      chint.textContent = cauto ? 'Auto: a distinct color is picked for you.' : 'Custom color: ' + cinp.value;
    }
    PRESETS.forEach(function(hex){
      var sw = el('button', 'sw'); sw.type = 'button'; sw.style.background = hex; sw.title = hex;
      sw.addEventListener('click', function(){ cinp.value = hex; cauto = false; paintColor(); });
      crow.appendChild(sw);
    });
    cinp.addEventListener('input', function(){ cauto = false; paintColor(); });
    autoBtn.type = 'button';
    autoBtn.addEventListener('click', function(){ cauto = true; cinp.value = autoColor(autoIdx); paintColor(); });
    crow.appendChild(cinp); crow.appendChild(autoBtn);
    modal.appendChild(crow); modal.appendChild(chint); paintColor();
    f.color = { get value(){ return cauto ? '' : cinp.value; } };
    // Beep macros: switch off for a printer that doesn't have them installed (the scanner works either way)
    modal.appendChild(el('label', '', 'Beep macros (optional)'));
    var bl = el('label', 'chk'), bchk = el('input');
    bchk.type = 'checkbox'; bchk.checked = !(existing && existing.beeps_off);
    bl.appendChild(bchk); bl.appendChild(el('span', '', 'Run the beep macros on this printer'));
    modal.appendChild(bl);
    modal.appendChild(el('div', 'hint', 'Only matters if you set up beeps (see docs/feedback.md). Untick it for a printer that does not have the macros installed. Scanning works either way.'));
    f.beeps_off = { get value(){ return !bchk.checked; } };
    add('api_key', 'Moonraker API key (optional)', existing ? 'Leave blank to keep the current key.' : '', 'password', existing && existing.has_api_key ? '(unchanged)' : '');
    loadLocations();
    var res = el('div', 'res'); res.style.display = 'none';
    function body(){ var o = {}; Object.keys(f).forEach(function(k){ o[k] = f[k].value; }); return o; }
    function show(ok, text){ res.style.display = ''; res.className = 'res ' + (ok ? 'good' : 'bad'); res.textContent = text; }
    function guard(r){ if (r.status === 401){ token = ''; showToken('Wrong token, try again.'); return true; } return false; }
    var b = el('div', 'btns');
    b.appendChild(btn('Auto detect', function(){
      show(true, 'Detecting…');
      var o = body(); if (existing) o.original = existing.name;
      api('POST', 'api/printers/test', o).then(function(r){
        if (guard(r)) return;
        var d = r.data;
        if (r.status !== 200) return show(false, d.error || 'Error');
        var filled = [];
        function fill(key, val, label){ if (val && !f[key].value.trim()){ f[key].value = val; filled.push(label); } }
        if (d.moonraker) f.moonraker.value = d.moonraker;
        if (!locLoaded) loadLocations();
        fill('name', d.name, 'name'); fill('snapshot_url', d.snapshot_url, 'snapshot URL'); fill('webcam', d.webcam, 'webcam');
        var found = d.klippy_state ? ('Moonraker ' + (d.moonraker_version || '') + ' (klippy ' + d.klippy_state + ')') : '';
        if (d.image) found += ', camera ' + d.image;
        var tail = filled.length ? '\nFilled in: ' + filled.join(', ') : '';
        show(d.ok, d.ok ? ('OK: ' + found + tail) : ((d.error || 'Failed') + (found ? '\n(' + found + ')' : '') + tail));
      });
    }));
    b.appendChild(btn('Save', function(){
      var p = existing ? api('PUT', 'api/printers/' + encodeURIComponent(existing.name), body()) : api('POST', 'api/printers', body());
      p.then(function(r){
        if (guard(r)) return;
        if (r.status !== 200) return show(false, r.data.error || 'Error');
        showList(); pollStatus();
      });
    }));
    b.appendChild(btn('Back', showList));
    modal.appendChild(b); modal.appendChild(res);
    f.name.focus();
  }

  manageBtn.addEventListener('click', showList);
  mbg.addEventListener('click', function(e){ if (e.target === mbg) closeModal(); });

  document.getElementById('textmode').addEventListener('click', function(){
    textMode = textMode === 'scroll' ? 'wrap' : 'scroll'; store('text', textMode); applyTextMode();
  });
  var rz; window.addEventListener('resize', function(){ clearTimeout(rz); rz = setTimeout(layoutMarquee, 150); });
  document.getElementById('lfilter').addEventListener('change', function(e){ lfilter = e.target.value; store('level', lfilter); render(); });
  document.getElementById('pause').addEventListener('click', function(e){ paused = !paused; e.target.textContent = paused ? 'Resume' : 'Pause'; if (!paused) pollLog(); });
  document.getElementById('clear').addEventListener('click', function(){ buffer = []; render(); });

  applyTextMode();
  pollLog(); pollStatus();
  setInterval(pollLog, 1000);
  setInterval(pollStatus, 3000);
})();
</script>
</body>
</html>
"""


def normalize_moonraker(raw):
    """'192.168.1.5' -> 'http://192.168.1.5:7125'; full URLs are kept as given."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Moonraker address is required")
    had_scheme = "://" in raw
    if not had_scheme:
        raw = "http://" + raw
    u = urlparse(raw)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Moonraker address must look like http://192.168.1.10:7125")
    raw = raw.rstrip("/")
    if not had_scheme and u.port is None:
        raw += ":7125"
    return raw


MACRO_EVENTS = ("assigned", "cleared", "not_found")   # the events that have their own macro; the others reuse these


def clean_printer_fields(body):
    """Validate the fields the web form can edit. Returns a dict with all keys present
    (empty string = not set). Raises ValueError with a message fit to show the user."""
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 60 or re.search(r"[\x00-\x1f]", name):
        raise ValueError("Name is required (max 60 characters, no control characters)")
    out = {"name": name, "moonraker": normalize_moonraker(body.get("moonraker"))}
    for key in ("snapshot_url", "webcam", "location", "api_key"):
        v = str(body.get(key) or "").strip()
        if len(v) > 300:
            raise ValueError(f"{key} is too long")
        out[key] = v
    if out["snapshot_url"] and not re.match(r"https?://[^\s/]+", out["snapshot_url"]):
        raise ValueError("Snapshot URL must start with http:// or https://")
    color = str(body.get("color") or "").strip().lower()
    if color and not re.fullmatch(r"#[0-9a-f]{6}", color):
        raise ValueError("Color must look like #e69f00")
    out["color"] = color
    out["beeps_off"] = bool(body.get("beeps_off"))
    return out


class PrinterManager:
    """Owns the running Printer threads and the printers list in config.json.
    Add / update / remove take effect immediately and are written back to the file."""

    def __init__(self, cfg_path, settings, dry_run=False):
        self.cfg_path = cfg_path
        self.settings = settings
        self.dry_run = dry_run
        self.printers = []
        self.lock = threading.RLock()
        self._backed_up = False

    def _make(self, cfg):
        return Printer(cfg, self.settings, self.printers, dry_run=self.dry_run)

    def load(self, printer_cfgs):
        for c in printer_cfgs:
            self.printers.append(self._make(c))

    def start_all(self):
        for p in self.printers:
            p.start()

    def _find(self, name):
        for p in self.printers:
            if p.name_ == name:
                return p
        raise KeyError(name)

    def _write(self, cfgs):
        """Rewrite only the "printers" list, keeping every other setting in the file."""
        with open(self.cfg_path) as f:
            data = json.load(f)
        data["printers"] = cfgs
        if not self._backed_up:
            shutil.copy2(self.cfg_path, self.cfg_path + ".bak")
            self._backed_up = True
        tmp = self.cfg_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, self.cfg_path)

    @staticmethod
    def _apply(cfg, fields):
        cfg.update(name=fields["name"], moonraker=fields["moonraker"])
        for key in ("snapshot_url", "webcam", "location", "color"):
            if fields[key]:
                cfg[key] = fields[key]
            else:
                cfg.pop(key, None)
        if fields["api_key"]:          # blank = keep the existing key
            cfg["api_key"] = fields["api_key"]
        # "Beep macros" switch: empty macro names in this printer's feedback.gcode turn them off for it
        fb = dict(cfg.get("feedback") or {})
        gc = dict(fb.get("gcode") or {})
        if fields["beeps_off"]:
            gc.update({e: "" for e in MACRO_EVENTS})
        else:
            for e in MACRO_EVENTS:
                if gc.get(e) == "":
                    del gc[e]
        if gc:
            fb["gcode"] = gc
        else:
            fb.pop("gcode", None)
        if fb:
            cfg["feedback"] = fb
        else:
            cfg.pop("feedback", None)
        return cfg

    def public_config(self):
        with self.lock:
            out = []
            for p in self.printers:
                c = p.cfg
                out.append({"name": c["name"], "moonraker": c["moonraker"],
                            "snapshot_url": c.get("snapshot_url", ""), "webcam": c.get("webcam", ""),
                            "location": c.get("location", ""), "color": c.get("color", ""),
                            "beeps_off": all(((c.get("feedback") or {}).get("gcode") or {}).get(e) == ""
                                             for e in MACRO_EVENTS),
                            "has_api_key": bool(c.get("api_key"))})
            return out

    def add(self, body):
        with self.lock:
            f = clean_printer_fields(body)
            if any(p.name_ == f["name"] for p in self.printers):
                raise ValueError(f"A printer named '{f['name']}' already exists")
            cfg = self._apply({}, f)
            self._write([p.cfg for p in self.printers] + [cfg])
            p = self._make(cfg)
            self.printers.append(p)
            p.start()
            log.info("printer added: %s (%s)", cfg["name"], cfg["moonraker"])

    def update(self, original, body):
        with self.lock:
            old = self._find(original)
            f = clean_printer_fields(body)
            if f["name"] != original and any(p.name_ == f["name"] for p in self.printers):
                raise ValueError(f"A printer named '{f['name']}' already exists")
            cfg = self._apply(dict(old.cfg), f)
            idx = self.printers.index(old)
            cfgs = [p.cfg for p in self.printers]
            cfgs[idx] = cfg
            self._write(cfgs)
            old.stop_event.set()
            new = self._make(cfg)
            self.printers[idx] = new
            new.start()
            log.info("printer updated: %s (%s)", cfg["name"], cfg["moonraker"])

    def remove(self, name):
        with self.lock:
            p = self._find(name)
            self._write([q.cfg for q in self.printers if q is not p])
            p.stop_event.set()
            self.printers.remove(p)
            log.info("printer removed: %s", name)

    def locations(self, moonraker=""):
        """Spoolman's locations, read through the first printer that answers."""
        tried = list(self.printers)[:3]
        if moonraker.strip():
            lookup = Printer({"name": "lookup", "moonraker": normalize_moonraker(moonraker)},
                             self.settings, [], dry_run=True)
            lookup.quiet = True
            tried.append(lookup)
        errors = []
        for p in tried:
            try:
                return {"locations": p.spoolman_locations(), "source": p.name_}
            except Exception as e:
                errors.append(f"{p.name_}: {e}")
        raise ValueError("Could not read locations from Spoolman (" + (
            "; ".join(errors) or "no printers yet: type a Moonraker address first") + ")")

    def test(self, body):
        """Try a printer definition without saving it: Moonraker, Spoolman component, camera."""
        body = dict(body) if isinstance(body, dict) else {}
        body["name"] = body.get("name") or "test"
        f = clean_printer_fields(body)
        original = body.get("original")
        if not f["api_key"] and original:
            try:
                f["api_key"] = self._find(original).cfg.get("api_key", "")
            except KeyError:
                pass
        cfg = {"name": "test", "moonraker": f["moonraker"]}
        for key in ("snapshot_url", "webcam", "api_key"):
            if f[key]:
                cfg[key] = f[key]
        p = Printer(cfg, self.settings, [], dry_run=True)
        p.quiet = True
        res = {"ok": False, "moonraker": f["moonraker"]}
        try:
            info = p.moon("GET", "/server/info")
        except Exception as e:
            res["error"] = f"Moonraker: {e}"
            return res
        res["klippy_state"] = info.get("klippy_state")
        res["moonraker_version"] = info.get("moonraker_version")
        try:  # Klipper's hostname makes a handy default name
            res["name"] = str(p.moon("GET", "/printer/info").get("hostname") or "")
        except Exception:
            pass
        problem = ""
        if "spoolman" not in (info.get("components") or []):
            problem = "Moonraker is reachable but has no [spoolman] component configured"
        try:
            url = p.snapshot_url or p.discover_snapshot()
            p.snapshot_url = url
            h, w = p.grab().shape[:2]
            res.update(snapshot_url=url, image=f"{w}x{h}", webcam=getattr(p, "detected_webcam", ""))
        except Exception as e:
            problem = problem or f"Camera: {e}"
        if problem:
            res["error"] = problem
        else:
            res["ok"] = True
        return res


class WebServer:
    """Small HTTP server: live log page + JSON endpoints.

    Read-only endpoints need no authentication. Managing printers (add / update / remove)
    is off unless web.allow_edit is true. If web.edit_token is also set, every edit request
    must carry 'Authorization: Bearer <edit_token>'. Edit requests always need the header
    'X-Scanner-Client: web', which a foreign website can't send without a CORS preflight
    (never answered), so other sites can't drive the API through your browser."""

    def __init__(self, cfg, buf, manager, started):
        self.host = cfg.get("host", "0.0.0.0")
        self.port = int(cfg.get("port", 8090))
        token = str(cfg.get("edit_token") or "")
        self.editable = bool(cfg.get("allow_edit"))
        self.token_required = bool(token)
        if self.editable and not token:
            log.warning("printer editing is ON without web.edit_token: anyone who can reach this "
                        "port can change the printer list (fine on a trusted home network)")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype, cors=True):
                data = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                if cors:
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)

            def _json(self, obj, code=200, cors=True):
                self._send(code, json.dumps(obj), "application/json; charset=utf-8", cors)

            def _authorized(self):
                """Guard for write endpoints. Sends the error response itself when refusing."""
                if not outer.editable:
                    self._json({"error": "Editing is disabled (set web.allow_edit to true)"},
                               403, cors=False)
                    return False
                if self.headers.get("X-Scanner-Client") != "web":
                    self._json({"error": "Missing X-Scanner-Client header"}, 403, cors=False)
                    return False
                if not token:
                    return True
                got = self.headers.get("Authorization", "")
                if not hmac.compare_digest(got.encode(), ("Bearer " + token).encode()):
                    time.sleep(0.5)  # slow down guessing
                    self._json({"error": "Wrong or missing token"}, 401, cors=False)
                    return False
                return True

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n > 65536:
                    raise ValueError("request too large")
                return json.loads(self.rfile.read(n) or b"{}")

            def _managed(self, fn):
                """Run a manager action and translate errors to JSON responses."""
                try:
                    result = fn()
                    self._json(result if result is not None else {"ok": True}, cors=False)
                except KeyError as e:
                    self._json({"error": f"No such printer: {e.args[0]}"}, 404, cors=False)
                except (ValueError, json.JSONDecodeError) as e:
                    self._json({"error": str(e)}, 400, cors=False)
                except OSError as e:
                    self._json({"error": f"Could not save config file: {e}"}, 500, cors=False)

            def _printer_name(self, path):
                return unquote(path[len("/api/printers/"):])

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path in ("/", "/index.html"):
                    self._send(200, PAGE_HTML, "text/html; charset=utf-8")
                elif u.path == "/api/log":
                    try:
                        after = int(q.get("after", ["0"])[0])
                        limit = min(int(q.get("limit", ["500"])[0]), 1000)
                    except ValueError:
                        return self._json({"error": "bad parameter"}, 400)
                    lines, last = buf.since(after, limit)
                    self._json({"lines": lines, "last_id": last})
                elif u.path == "/api/status":
                    self._json({"version": __version__, "uptime_seconds": int(time.time() - started),
                                "editable": outer.editable, "token_required": outer.token_required,
                                "printers": [p.status() for p in list(manager.printers)]})
                elif u.path == "/api/summary":
                    sts = [p.status() for p in list(manager.printers)]
                    ev = [(s["last_event"]["time"], s["name"], s["last_event"]["message"])
                          for s in sts if s["last_event"]]
                    last = max(ev)[1:] if ev else None
                    self._json({
                        "printers_total": len(sts),
                        "printers_online": sum(1 for s in sts if s["camera"] == "ok"),
                        "printers_with_spool": sum(1 for s in sts if s["spool"] is not None),
                        "last_event": f"{last[0]}: {last[1]}" if last else "none yet",
                        "uptime_seconds": int(time.time() - started),
                        "version": __version__})
                elif u.path == "/api/locations":
                    if self._authorized():
                        self._managed(lambda: manager.locations(q.get("moonraker", [""])[0]))
                elif u.path == "/api/config":
                    if self._authorized():
                        self._json({"printers": manager.public_config()}, cors=False)
                elif u.path == "/health":
                    self._send(200, "ok", "text/plain")
                else:
                    self._send(404, "not found", "text/plain")

            def do_POST(self):
                path = urlparse(self.path).path
                if path not in ("/api/printers", "/api/printers/test"):
                    return self._send(404, "not found", "text/plain")
                if not self._authorized():
                    return
                if path == "/api/printers/test":
                    return self._managed(lambda: manager.test(self._body()))
                self._managed(lambda: manager.add(self._body()))

            def do_PUT(self):
                path = urlparse(self.path).path
                if not path.startswith("/api/printers/"):
                    return self._send(404, "not found", "text/plain")
                if self._authorized():
                    self._managed(lambda: manager.update(self._printer_name(path), self._body()))

            def do_DELETE(self):
                path = urlparse(self.path).path
                if not path.startswith("/api/printers/"):
                    return self._send(404, "not found", "text/plain")
                if self._authorized():
                    self._managed(lambda: manager.remove(self._printer_name(path)))

        outer = self
        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="web").start()
        editing = "off"
        if self.editable:
            editing = "ENABLED (token required)" if self.token_required else "ENABLED (no token)"
        log.info("web page listening on http://%s:%s/ (printer editing %s)", self.host, self.port, editing)



def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true", help="log what would happen, change nothing")
    ap.add_argument("--decode-file", help="decode a saved snapshot and exit (for testing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"spool_cam_scanner {__version__}")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.decode_file:
        img = cv2.imread(args.decode_file)
        if img is None:
            sys.exit("could not read image")
        texts = Decoder().decode(img)
        for t in texts:
            print(repr(t), "->", parse_spool_id(t))
        sys.exit(0 if texts else "no QR code found")

    with open(args.config) as f:
        cfg = json.load(f)
    web_cfg = cfg.get("web") or {}
    buf = LogBuffer(int(web_cfg.get("max_lines", 1000)))
    buf.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger().addHandler(buf)
    log.info("spool_cam_scanner %s starting", __version__)
    settings = {
        "poll_interval": cfg.get("poll_interval", 0.5),   # seconds between snapshots
        "cooldown": cfg.get("cooldown", 10),              # seconds a spool must be out of view to re-trigger
        "skip_while_printing": cfg.get("skip_while_printing", True),
        "exclusive": cfg.get("exclusive", True),          # clear the spool from the printer it left
        "set_location": cfg.get("set_location", True),    # set Spoolman location = printer when assigned
        "feedback": cfg.get("feedback") or {},
        "motion_gate": cfg.get("motion_gate", True),          # only decode when the picture changes
        "motion_threshold": cfg.get("motion_threshold", 1.5), # mean pixel change that counts as motion
        "motion_hold": cfg.get("motion_hold", 3.0),           # keep decoding this long after motion
        "busy_poll_interval": cfg.get("busy_poll_interval", 2.0),  # poll delay while printing
        "storage_location": cfg.get("storage_location"),  # location to give a spool when CLEARed (or null)
    }
    manager = PrinterManager(args.config, settings, dry_run=args.dry_run)
    manager.load(cfg["printers"])
    manager.start_all()
    if web_cfg.get("enabled"):
        try:
            WebServer(web_cfg, buf, manager, time.time()).start()
        except OSError as e:
            log.error("could not start web page on port %s: %s", web_cfg.get("port", 8090), e)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
