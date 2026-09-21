import cv2
import numpy as np
import pytest

import spool_cam_scanner as sc
from helpers import make_frame


def decode(jpg):
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    return sc.Decoder().decode(img)


@pytest.mark.parametrize("px", [300, 150, 100])
def test_decodes_various_sizes(px):
    assert decode(make_frame("web+spoolman:s-42", qr_px=px)) == ["web+spoolman:s-42"]


def test_blank_frame_finds_nothing():
    assert decode(make_frame(None)) == []


def test_clear_label_decodes():
    assert decode(make_frame("SM:CLEAR")) == ["SM:CLEAR"]
