import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import threading

import pytest


@pytest.fixture(autouse=True)
def _stop_printer_threads():
    """Make sure no scanner thread outlives its test (a thread still inside OpenCV when the
    interpreter exits makes the process abort)."""
    yield
    import spool_cam_scanner as sc
    alive = [t for t in threading.enumerate() if isinstance(t, sc.Printer)]
    for t in alive:
        t.stop_event.set()
    for t in alive:
        t.join(timeout=5)
