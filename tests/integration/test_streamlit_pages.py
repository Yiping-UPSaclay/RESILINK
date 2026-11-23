"""Streamlit AppTest smoke tests (pages render without exceptions; AI-off home degrades gracefully)."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[2] / "apps" / "streamlit"


def test_home_without_ai(monkeypatch):
    monkeypatch.setenv("RESILINK_AI", "off")
    at = AppTest.from_file(str(APP / "Home.py"), default_timeout=60).run()
    assert not at.exception
    assert any("AI is off" in w.value for w in at.warning)


@pytest.mark.usefixtures("small_run")
def test_risk_page_renders():
    at = AppTest.from_file(str(APP / "pages" / "1_Risk.py"), default_timeout=120).run()
    assert not at.exception, at.exception


def test_ops_console_renders():
    at = AppTest.from_file(str(APP / "pages" / "3_Ops_console.py"), default_timeout=60).run()
    assert not at.exception, at.exception


def test_review_page_renders():
    at = AppTest.from_file(str(APP / "pages" / "2_Review.py"), default_timeout=60).run()
    assert not at.exception, at.exception
