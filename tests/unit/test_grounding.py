from __future__ import annotations

from resilink_ai.grounding import check

REF = [{"rows": [{"metric": "eal", "sector": "all", "value": 217624.13, "p05": 207766.4, "p95": 227482.0}],
        "assets": [{"rid": "telecom.mobile_site:syn:S21", "p_down": 0.478}], "scenario_id": "flood_rp100"}]


def test_exact_rounded_and_range_values_are_grounded():
    rep = check("EAL is 217,624 people-hours/year (5-95 %: 207,766-227,482), roughly 218k.", REF)
    assert rep.ok, rep.as_dict()


def test_percentages_from_fractions_and_identifiers_are_ignored():
    rep = check("Site S21 is down with probability 47.8 % in flood_rp100 (the 100-year flood).", REF)
    assert rep.ok, rep.as_dict()


def test_fabricated_numbers_are_flagged():
    rep = check("The EAL is 123,456 people-hours and 31.2 % of cells are disrupted.", REF)
    assert not rep.ok
    assert set(rep.ungrounded) == {"123,456", "31.2 %"}


def test_french_decimal_comma():
    rep = check("La probabilité est de 0,478.", REF)
    assert rep.ok, rep.as_dict()
