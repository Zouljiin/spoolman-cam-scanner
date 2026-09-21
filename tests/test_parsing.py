import pytest

import spool_cam_scanner as sc


@pytest.mark.parametrize("text,expected", [
    ("web+spoolman:s-42", 42),
    ("WEB+SPOOLMAN:S-7", 7),
    ("SM:SPOOL=15", 15),
    ("https://spoolman.lan:7912/spool/show/99", 99),
    ("SM:CLEAR", sc.CLEAR),
    ("web+spoolman:clear", sc.CLEAR),
    ("https://example.com", None),
    ("", None),
    ("web+spoolman:f-3", None),   # a *filament* label must not be taken as a spool
])
def test_parse_spool_id(text, expected):
    assert sc.parse_spool_id(text) == expected


def test_spool_desc():
    info = {"filament": {"name": "Blue", "material": "PLA+", "vendor": {"name": "GST3D"}}}
    assert sc.spool_desc(info) == "GST3D Blue PLA+"
    assert sc.spool_desc({}) == ""
    assert sc.spool_desc(None) == ""
