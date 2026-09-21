"""End-to-end: the real scanner threads against fake Moonraker printers."""
import pytest

import spool_cam_scanner as sc
from helpers import MockPrinter, make_frame, wait_for

BLANK = make_frame(None)
PORTS = iter(range(19100, 19400))


def settings(**over):
    s = {"poll_interval": 0.05, "cooldown": 0.5, "skip_while_printing": True, "exclusive": True,
         "set_location": True, "storage_location": "Shelf", "feedback": {},
         "motion_gate": True, "motion_threshold": 1.5, "motion_hold": 3.0,
         "busy_poll_interval": 0.1}
    s.update(over)
    return s


@pytest.fixture
def rig():
    made, threads = [], []

    def build(*specs, **over):
        """specs: (name, frame, state, active). Returns (mocks, scanners)."""
        reg, mocks = [], []
        st = settings(**over)
        for name, frame, state, active, *extra in specs:
            m = MockPrinter(next(PORTS), frame, state, active)
            mocks.append(m)
            made.append(m)
            reg.append(sc.Printer({"name": name, "moonraker": m.url, **(extra[0] if extra else {})}, st, reg))
        for r in reg:
            r.start()
            threads.append(r)
        return mocks, reg

    yield build
    for r in threads:
        r.stop_event.set()
    for m in made:
        m.stop()


def label(text):
    return make_frame(text)


def test_assign_sets_spool_and_location(rig):
    (a,), _ = rig(("Ender_A", label("web+spoolman:s-41"), "standby", None))
    assert wait_for(lambda: a.active == 41)
    assert wait_for(lambda: a.locations.get(41) == "Ender_A")
    assert wait_for(lambda: any("GST3D Blue PLA+" in m for m in a.messages()))


def test_unknown_spool_is_rejected(rig):
    (a,), _ = rig(("Ender_A", label("web+spoolman:s-999"), "standby", None))
    assert wait_for(lambda: any("not found" in m for m in a.messages()))
    assert a.active is None and a.sets() == []


def test_ignored_while_printing(rig):
    (a,), _ = rig(("Ender_A", label("web+spoolman:s-41"), "printing", None))
    import time
    time.sleep(1.5)
    assert a.active is None and a.calls == []   # nothing at all sent to a printing printer


@pytest.mark.parametrize("state", ["standby", "paused", "complete", "cancelled", "error", ""])
def test_scans_are_accepted_in_every_state_except_printing(rig, state):
    """'paused' is what Klipper reports during a colour change (M600 macro -> PAUSE)."""
    (a,), _ = rig(("Ender_A", label("web+spoolman:s-41"), state, None))
    assert wait_for(lambda: a.active == 41), f"scan was not accepted in state {state!r}"


def test_colour_change_flow_ignored_while_printing_then_accepted_when_paused(rig):
    (a,), _ = rig(("Ender_A", label("web+spoolman:s-41"), "printing", None))
    import time
    time.sleep(1.0)
    assert a.active is None and a.calls == []          # ignored while printing, nothing sent
    a.state = "paused"                                  # M600 -> PAUSE
    a.frame = BLANK                                     # take the spool away ...
    time.sleep(0.9)                                     # ... for longer than the cooldown
    a.frame = label("web+spoolman:s-41")                # ... and show it again
    assert wait_for(lambda: a.active == 41)
    a.state = "printing"                                # RESUME
    a.frame = BLANK
    time.sleep(0.9)
    a.frame = label("web+spoolman:s-42")
    time.sleep(1.0)
    assert a.active == 41                               # a scan after RESUME is ignored again


def test_already_active_is_a_noop(rig):
    (a,), _ = rig(("Ender_A", label("web+spoolman:s-41"), "standby", 41))
    assert wait_for(lambda: any("already active" in m for m in a.messages()))
    assert a.sets() == []


def test_clear_label_clears_and_resets_location(rig):
    (a,), _ = rig(("Ender_A", label("SM:CLEAR"), "standby", 41))
    assert wait_for(lambda: a.active is None and a.sets() == [None])
    assert a.locations.get(41) == "Shelf"
    assert wait_for(lambda: any("removed (clear label scanned)" in m for m in a.messages()))


def test_moving_spool_releases_it_from_the_other_printer(rig):
    (a, b), _ = rig(("Ender_A", label("web+spoolman:s-42"), "standby", None),
                    ("Ender_B", BLANK, "standby", 42))
    assert wait_for(lambda: a.active == 42 and b.active is None)
    assert wait_for(lambda: any("moved to Ender_A" in m for m in b.messages()))


def test_exclusive_never_touches_a_printing_printer(rig):
    (a, b), _ = rig(("Ender_A", label("web+spoolman:s-42"), "standby", None),
                    ("Ender_B", BLANK, "printing", 42))
    assert wait_for(lambda: a.active == 42)
    import time
    time.sleep(0.5)
    assert b.active == 42 and b.calls == []


def test_replacing_a_spool_resets_the_old_ones_location(rig):
    (a,), _ = rig(("Ender_A", label("web+spoolman:s-42"), "standby", 7))
    assert wait_for(lambda: a.active == 42)
    assert wait_for(lambda: a.locations.get(7) == "Shelf")
    assert wait_for(lambda: any("spool #7 removed" in m for m in a.messages()))


def test_feedback_macro_and_popup(rig):
    fb = {"popup": True, "popup_seconds": 0, "gcode": {"assigned": "SPOOL_SCAN_OK"}}
    (a,), _ = rig(("Ender_A", label("web+spoolman:s-41"), "standby", None), feedback=fb)
    assert wait_for(lambda: any(k == "gcode" and v.startswith("SPOOL_SCAN_OK SPOOL=41")
                                for k, v in a.calls))
    assert wait_for(lambda: any(k == "gcode" and "prompt_begin" in v for k, v in a.calls))


def test_per_printer_override_with_empty_names_switches_a_macro_off(rig):
    fb = {"gcode": {"assigned": "SPOOL_SCAN_OK"}}
    off = {"feedback": {"gcode": {"assigned": ""}}}
    (a, b), _ = rig(("Ender_A", label("web+spoolman:s-41"), "standby", None),
                    ("Ender_B", label("web+spoolman:s-42"), "standby", None, off), feedback=fb)
    macro_calls = lambda m: [v for k, v in m.calls if k == "gcode" and v.startswith("SPOOL_SCAN_OK")]
    assert wait_for(lambda: macro_calls(a))                          # global setting works
    assert wait_for(lambda: b.active == 42 and any("Active spool set" in x for x in b.messages()))
    assert macro_calls(b) == []                                      # switched off for this printer


def test_status_reports_camera_and_spool(rig):
    (a,), (p,) = rig(("Ender_A", label("web+spoolman:s-41"), "standby", None))
    assert wait_for(lambda: p.status()["spool"] == 41 and p.status()["camera"] == "ok"
                    and p.status()["last_event"] is not None)
    st = p.status()
    assert st["spool_desc"] == "GST3D Blue PLA+" and st["last_event"]["event"] == "assigned"
