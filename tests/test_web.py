import json
import logging
import os
import time
import urllib.error
import urllib.request

import pytest

import spool_cam_scanner as sc
from helpers import MockPrinter, make_frame, wait_for

SETTINGS = {"poll_interval": 0.05, "cooldown": 0.5, "skip_while_printing": True, "exclusive": True,
            "set_location": True, "storage_location": None, "feedback": {}, "motion_gate": True,
            "motion_threshold": 1.5, "motion_hold": 3.0, "busy_poll_interval": 0.1}


def call(port, path, method="GET", body=None, token=None, client=True):
    """Returns (status, headers, parsed-or-raw body)."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if client:   # what the web page always sends
        req.add_header("X-Scanner-Client", "web")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw, status, hdr = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, status, hdr = e.read(), e.code, e.headers
    try:
        return status, hdr, json.loads(raw)
    except ValueError:
        return status, hdr, raw


@pytest.fixture
def stack(tmp_path):
    """A manager + web server on real threads, with one fake printer and a real config file."""
    made = []

    def build(port, web_cfg=None, extra_cfg=None):
        buf = sc.LogBuffer(50)
        root = logging.getLogger()
        root.addHandler(buf)
        root.setLevel(logging.INFO)
        m = MockPrinter(port + 1, make_frame("web+spoolman:s-41"))
        cfg_path = tmp_path / "config.json"
        printers = [{"name": "Ender_A", "moonraker": m.url}]
        cfg_path.write_text(json.dumps({"storage_location": "Shelf", "printers": printers,
                                        **(extra_cfg or {})}))
        mgr = sc.PrinterManager(str(cfg_path), SETTINGS)
        mgr.load(printers)
        mgr.start_all()
        web = sc.WebServer({"host": "127.0.0.1", "port": port, **(web_cfg or {})}, buf, mgr, time.time())
        web.start()
        made.append((root, buf, mgr, web, m))
        return mgr, web, m, cfg_path

    yield build
    for root, buf, mgr, web, m in made:
        for p in mgr.printers:
            p.stop_event.set()
        web.httpd.shutdown()
        web.httpd.server_close()
        m.stop()
        root.removeHandler(buf)


def test_read_only_endpoints(stack):
    mgr, web, m, _ = stack(19502)
    assert wait_for(lambda: m.active == 41)
    code, hdr, body = call(19502, "/")
    assert code == 200 and b"Spoolman Cam Scanner" in body and "text/html" in hdr["Content-Type"]

    _, _, d = call(19502, "/api/log?after=0")
    assert any(l["printer"] == "Ender_A" and "setting active spool" in l["msg"] for l in d["lines"])
    assert call(19502, f"/api/log?after={d['last_id']}")[2]["lines"] == []

    assert wait_for(lambda: call(19502, "/api/status")[2]["printers"][0]["spool"] == 41)
    s = call(19502, "/api/status")[2]
    assert s["printers"][0]["name"] == "Ender_A" and s["printers"][0]["camera"] == "ok"
    assert s["editable"] is False

    summ = call(19502, "/api/summary")[2]
    assert summ["printers_total"] == 1 and summ["printers_online"] == 1
    assert summ["printers_with_spool"] == 1 and "Ender_A" in summ["last_event"]

    assert call(19502, "/health")[0] == 200
    assert call(19502, "/nope")[0] == 404
    assert call(19502, "/api/status")[1]["Access-Control-Allow-Origin"] == "*"
    assert "X-Frame-Options" not in call(19502, "/")[1]   # must stay embeddable


def test_editing_is_off_by_default(stack):
    stack(19510)
    for method, path, body in [("GET", "/api/config", None),
                               ("POST", "/api/printers", {"name": "X", "moonraker": "1.2.3.4"}),
                               ("PUT", "/api/printers/Ender_A", {"name": "X", "moonraker": "1.2.3.4"}),
                               ("DELETE", "/api/printers/Ender_A", None)]:
        assert call(19510, path, method, body, token="anything")[0] == 403


def test_edit_without_token_works_but_needs_the_client_header(stack):
    mgr, web, _, _ = stack(19512, {"allow_edit": True})
    st = call(19512, "/api/status")[2]
    assert st["editable"] is True and st["token_required"] is False
    assert call(19512, "/api/config")[0] == 200
    add = {"name": "Ender_X", "moonraker": "1.2.3.4"}
    # a request from another website (no custom header) is refused
    assert call(19512, "/api/printers", "POST", add, client=False)[0] == 403
    assert call(19512, "/api/config", client=False)[0] == 403
    assert [p.name_ for p in mgr.printers] == ["Ender_A"]
    assert call(19512, "/api/printers", "POST", add)[0] == 200
    assert [p.name_ for p in mgr.printers] == ["Ender_A", "Ender_X"]


def test_wrong_or_missing_token_is_rejected(stack):
    stack(19514, {"allow_edit": True, "edit_token": "s3cret"})
    st = call(19514, "/api/status")[2]
    assert st["editable"] is True and st["token_required"] is True
    assert call(19514, "/api/config")[0] == 401
    assert call(19514, "/api/config", token="nope")[0] == 401
    assert call(19514, "/api/printers", "POST", {"name": "X", "moonraker": "1.2.3.4"}, token="nope")[0] == 401
    code, hdr, _ = call(19514, "/api/config", token="s3cret")
    assert code == 200 and "Access-Control-Allow-Origin" not in hdr   # write API is never cross-origin


def test_add_update_remove_persist_to_config_file(stack):
    mgr, _, _, cfg_path = stack(19516, {"allow_edit": True, "edit_token": "tok"})
    m2 = MockPrinter(19530, make_frame(None))
    try:
        # add (Moonraker address without scheme/port is normalised)
        code, _, d = call(19516, "/api/printers", "POST",
                          {"name": "Ender_B", "moonraker": "127.0.0.1:19530", "location": "Bench",
                           "color": "#E69F00"}, token="tok")
        assert code == 200, d
        assert [p.name_ for p in mgr.printers] == ["Ender_A", "Ender_B"]
        saved = json.loads(cfg_path.read_text())
        assert saved["storage_location"] == "Shelf"                    # other settings untouched
        assert saved["printers"][1] == {"name": "Ender_B", "moonraker": "http://127.0.0.1:19530",
                                        "location": "Bench", "color": "#e69f00"}   # colour is normalised
        assert call(19516, "/api/config", token="tok")[2]["printers"][1]["color"] == "#e69f00"
        assert wait_for(lambda: call(19516, "/api/status")[2]["printers"][1]["color"] == "#e69f00")
        assert call(19516, "/api/status")[2]["printers"][0]["color"] == ""             # unset = automatic
        assert (cfg_path.parent / "config.json.bak").exists()          # backup of the original
        assert wait_for(lambda: call(19516, "/api/status")[2]["printers"][1]["camera"] in ("ok", "error"))

        # duplicate name / invalid input
        assert call(19516, "/api/printers", "POST", {"name": "Ender_B", "moonraker": "1.2.3.4"}, token="tok")[0] == 400
        assert call(19516, "/api/printers", "POST", {"name": "", "moonraker": "1.2.3.4"}, token="tok")[0] == 400
        assert call(19516, "/api/printers", "POST", {"name": "Z", "moonraker": "ftp://x"}, token="tok")[0] == 400
        assert call(19516, "/api/printers", "POST",
                    {"name": "Z", "moonraker": "1.2.3.4", "snapshot_url": "nope"}, token="tok")[0] == 400
        for bad_color in ("red", "#12345", "#gggggg", "e69f00"):
            assert call(19516, "/api/printers", "POST",
                        {"name": "Z", "moonraker": "1.2.3.4", "color": bad_color}, token="tok")[0] == 400

        # update: rename + change location + add api key, extra keys preserved; empty colour = back to automatic
        code, _, d = call(19516, "/api/printers/Ender_B", "PUT",
                          {"name": "Ender_C", "moonraker": "127.0.0.1:19530", "location": "", "color": "",
                           "api_key": "k1"}, token="tok")
        assert code == 200, d
        assert [p.name_ for p in mgr.printers] == ["Ender_A", "Ender_C"]
        saved = json.loads(cfg_path.read_text())["printers"][1]
        assert saved == {"name": "Ender_C", "moonraker": "http://127.0.0.1:19530", "api_key": "k1"}
        # blank api_key on update keeps the key, and the key is never echoed back
        call(19516, "/api/printers/Ender_C", "PUT", {"name": "Ender_C", "moonraker": "127.0.0.1:19530"}, token="tok")
        assert json.loads(cfg_path.read_text())["printers"][1]["api_key"] == "k1"
        cfg = call(19516, "/api/config", token="tok")[2]["printers"][1]
        assert cfg["has_api_key"] is True and "api_key" not in cfg
        assert call(19516, "/api/printers/Nope", "PUT", {"name": "N", "moonraker": "1.2.3.4"}, token="tok")[0] == 404

        # remove
        assert call(19516, "/api/printers/Ender_C", "DELETE", token="tok")[0] == 200
        assert [p.name_ for p in mgr.printers] == ["Ender_A"]
        assert [p["name"] for p in json.loads(cfg_path.read_text())["printers"]] == ["Ender_A"]
        assert call(19516, "/api/printers/Ender_C", "DELETE", token="tok")[0] == 404
    finally:
        m2.stop()


def test_auto_detect_endpoint_reports_what_it_found(stack):
    _, _, m, _ = stack(19520, {"allow_edit": True, "edit_token": "tok"})
    port = m.port
    good = call(19520, "/api/printers/test", "POST", {"moonraker": f"127.0.0.1:{port}"}, token="tok")[2]
    assert good["ok"] is True and good["image"] == "1280x720" and good["klippy_state"] == "ready"
    assert good["snapshot_url"].endswith("/snap") and good["webcam"] == "cam"
    assert good["name"] == "ender-mock"                             # Klipper's hostname
    assert good["moonraker"] == f"http://127.0.0.1:{port}"          # normalised address

    m.components = ["webcam"]                                       # Moonraker without [spoolman]
    no_spool = call(19520, "/api/printers/test", "POST", {"moonraker": m.url}, token="tok")[2]
    assert no_spool["ok"] is False and "spoolman" in no_spool["error"]
    assert no_spool["snapshot_url"].endswith("/snap")               # still fills what it can

    bad = call(19520, "/api/printers/test", "POST", {"moonraker": "127.0.0.1:1"}, token="tok")[2]
    assert bad["ok"] is False and bad["error"].startswith("Moonraker:")
    assert call(19520, "/api/printers/test", "POST", {"moonraker": ""}, token="tok")[0] == 400
    assert call(19520, "/api/printers/test", "POST", {"moonraker": m.url})[0] == 401

    # the throw-away lookup printer must not leave "[test] ..." lines in the log
    lines = call(19520, "/api/log?after=0")[2]["lines"]
    assert not any(l["printer"] == "test" for l in lines)


def test_locations_dropdown_source(stack):
    _, _, m, _ = stack(19540, {"allow_edit": True})
    d = call(19540, "/api/locations")[2]
    assert d["locations"] == ["Bench", "dry box 1", "Shelf"]     # sorted, case-insensitive
    assert d["source"] == "Ender_A"

    m.location_endpoint = False                                   # fall back to the spools' own locations
    assert call(19540, "/api/locations")[2]["locations"] == ["Bench", "Shelf"]

    assert call(19540, "/api/locations", client=False)[0] == 403  # same guard as the other edit endpoints


def test_locations_can_use_a_typed_address_when_there_are_no_printers(stack):
    mgr, _, m, _ = stack(19542, {"allow_edit": True})
    call(19542, "/api/printers/Ender_A", "DELETE")
    assert mgr.printers == []
    code, _, d = call(19542, "/api/locations")
    assert code == 400 and "type a Moonraker address" in d["error"]
    code, _, d = call(19542, f"/api/locations?moonraker=127.0.0.1:{m.port}")
    assert code == 200 and "Shelf" in d["locations"] and d["source"] == "lookup"
    assert not any(l["printer"] == "lookup" for l in call(19542, "/api/log?after=0")[2]["lines"])


def _fields(**over):
    base = {"name": "P", "moonraker": "http://1.2.3.4:7125", "snapshot_url": "", "webcam": "", "location": "",
            "api_key": "", "color": "", "beeps_off": False}
    base.update(over)
    return base


def test_beeps_off_switch_writes_and_removes_empty_macro_names():
    apply = sc.PrinterManager._apply
    off = apply({}, _fields(beeps_off=True))
    assert off["feedback"] == {"gcode": {"assigned": "", "cleared": "", "not_found": ""}}
    on = apply(dict(off), _fields(beeps_off=False))
    assert "feedback" not in on                                   # nothing left over


def test_beeps_switch_keeps_other_feedback_settings_and_custom_macros():
    apply = sc.PrinterManager._apply
    cfg = {"name": "P", "moonraker": "x", "feedback": {"popup": False, "gcode": {"assigned": "MY_OK", "removed": "MY_GONE"}}}
    off = apply(cfg, _fields(beeps_off=True))
    assert off["feedback"]["popup"] is False
    assert off["feedback"]["gcode"] == {"assigned": "", "removed": "MY_GONE", "cleared": "", "not_found": ""}
    back = apply(dict(off, feedback=json.loads(json.dumps(off["feedback"]))), _fields(beeps_off=False))
    assert back["feedback"] == {"popup": False, "gcode": {"removed": "MY_GONE"}}   # other settings survive


def test_beeps_off_is_reported_and_editable_over_the_api(stack):
    mgr, _, _, cfg_path = stack(19544, {"allow_edit": True})
    assert call(19544, "/api/config")[2]["printers"][0]["beeps_off"] is False
    put = {"name": "Ender_A", "moonraker": mgr.printers[0].cfg["moonraker"], "beeps_off": True}
    assert call(19544, "/api/printers/Ender_A", "PUT", put)[0] == 200
    assert call(19544, "/api/config")[2]["printers"][0]["beeps_off"] is True
    saved = json.loads(cfg_path.read_text())["printers"][0]
    assert saved["feedback"]["gcode"] == {"assigned": "", "cleared": "", "not_found": ""}
    put["beeps_off"] = False
    assert call(19544, "/api/printers/Ender_A", "PUT", put)[0] == 200
    assert "feedback" not in json.loads(cfg_path.read_text())["printers"][0]


def test_example_config_does_not_call_macros_by_default():
    ex = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config.example.json")))
    assert "gcode" not in ex["feedback"]                     # a fresh printer has no SPOOL_SCAN_* macros
    assert ex["feedback"]["_gcode"]["assigned"] == "SPOOL_SCAN_OK"   # ...but the example shows how to switch them on


def test_normalize_moonraker():
    assert sc.normalize_moonraker("192.168.1.5") == "http://192.168.1.5:7125"
    assert sc.normalize_moonraker("192.168.1.5:7126/") == "http://192.168.1.5:7126"
    assert sc.normalize_moonraker("https://p.example.com") == "https://p.example.com"
    for bad in ("", "ftp://x", "http://"):
        with pytest.raises(ValueError):
            sc.normalize_moonraker(bad)
