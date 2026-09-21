"""Shared test helpers: synthetic camera frames and a fake Moonraker/Spoolman printer."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import cv2
import numpy as np
import qrcode


def make_frame(text=None, qr_px=300, w=1280, h=720, seed=0):
    """A JPEG 'camera frame'. With text -> contains a QR code of that text."""
    rng = np.random.default_rng(seed)
    bg = np.full((h, w, 3), (90, 110, 120), np.uint8)
    bg = cv2.add(bg, rng.integers(0, 25, bg.shape, dtype=np.uint8))
    if text:
        q = np.array(qrcode.make(text, box_size=6, border=2).convert("RGB"))[:, :, ::-1]
        q = cv2.resize(q, (qr_px, qr_px), interpolation=cv2.INTER_NEAREST)
        y, x = (h - qr_px) // 2, (w - qr_px) // 2
        bg[y:y + qr_px, x:x + qr_px] = q
    ok, buf = cv2.imencode(".jpg", bg)
    assert ok
    return buf.tobytes()


class MockPrinter:
    """Just enough of Moonraker (+ its Spoolman proxy) for the scanner."""

    SPOOLS = {
        7: {"id": 7, "filament": {"name": "Red", "material": "PLA", "vendor": {"name": "Acme"}}},
        41: {"id": 41, "filament": {"name": "Blue", "material": "PLA+", "vendor": {"name": "GST3D"}}},
        42: {"id": 42, "filament": {"name": "Green", "material": "PETG", "vendor": {"name": "Acme"}}},
    }

    def __init__(self, port, frame, state="standby", active=None):
        self.port, self.frame, self.state, self.active = port, frame, state, active
        self.components = ["spoolman", "webcam"]   # what /server/info reports
        self.hostname = "ender-mock"
        self.location_endpoint = True                 # False -> GET /v1/location fails (older Spoolman)
        self.location_names = ["Shelf", "dry box 1", "Bench"]
        self.spool_locations = {7: "Shelf", 41: "Bench", 42: "Shelf"}   # used by the /v1/spool fallback
        self.calls = []          # (kind, value) tuples, e.g. ("set", 41)
        self.locations = {}      # spool id -> last PATCHed location
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _j(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                p = urlparse(self.path).path
                if p == "/server/info":
                    return self._j({"result": {"klippy_state": "ready", "moonraker_version": "v0.11.0",
                                               "components": outer.components}})
                if p == "/printer/info":
                    return self._j({"result": {"state": "ready", "hostname": outer.hostname}})
                if p == "/server/webcams/list":
                    return self._j({"result": {"webcams": [{
                        "name": "cam", "enabled": True,
                        "snapshot_url": f"http://localhost:{outer.port}/snap"}]}})
                if p == "/snap":
                    b = outer.frame
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                    return
                if p == "/printer/objects/query":
                    return self._j({"result": {"status": {"print_stats": {"state": outer.state}}}})
                if p == "/server/spoolman/spool_id":
                    return self._j({"result": {"spool_id": outer.active}})
                self._j({}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                p = self.path
                if p == "/server/spoolman/spool_id":
                    if "spool_id" in body and body["spool_id"] is None:
                        return self._j({"error": "null not accepted"}, 400)  # like real Moonraker
                    outer.active = body.get("spool_id")
                    outer.calls.append(("set", outer.active))
                    return self._j({"result": {"spool_id": outer.active}})
                if p == "/server/spoolman/proxy":
                    if body["request_method"] == "GET" and body["path"] == "/v1/location":
                        if not outer.location_endpoint:
                            return self._j({"result": {"response": None,
                                                       "error": {"status_code": 404, "message": "no"}}})
                        return self._j({"result": {"response": outer.location_names, "error": None}})
                    if body["request_method"] == "GET" and body["path"] == "/v1/spool":
                        spools = [dict(sp, location=outer.spool_locations.get(i, ""))
                                  for i, sp in outer.SPOOLS.items()]
                        return self._j({"result": {"response": spools, "error": None}})
                    sid = int(body["path"].rsplit("/", 1)[1])
                    if body["request_method"] == "PATCH":
                        outer.locations[sid] = body["body"]["location"]
                        return self._j({"result": {"response": {"id": sid}, "error": None}})
                    if sid in outer.SPOOLS:
                        return self._j({"result": {"response": outer.SPOOLS[sid], "error": None}})
                    return self._j({"result": {"response": None,
                                               "error": {"status_code": 404, "message": "nope"}}})
                if p == "/printer/gcode/script":
                    outer.calls.append(("gcode", body["script"]))
                    return self._j({"result": "ok"})
                self._j({}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def sets(self):
        return [v for k, v in self.calls if k == "set"]

    def messages(self):
        return [v for k, v in self.calls if k == "gcode" and v.startswith("RESPOND MSG=")]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()   # refuse new connections instead of leaving them hanging


def wait_for(cond, timeout=6.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()
