"""Tests for flow.util pure helpers."""

from datetime import date

import pytest

from flow.util import (
    dte_from_expiry,
    et_today,
    mid,
    quote_side_bias,
    safe_float,
    safe_int,
    with_retries,
)


def test_safe_float():
    assert safe_float("1.5") == 1.5
    assert safe_float(None) is None
    assert safe_float("abc") is None
    assert safe_float(float("nan")) is None
    assert safe_float(float("inf")) is None


def test_safe_int():
    assert safe_int("7") == 7
    assert safe_int(3.9) == 3
    assert safe_int(None) is None
    assert safe_int("x") is None


def test_mid_prefers_bid_ask():
    assert mid(1.0, 1.4, 1.1) == pytest.approx(1.2)
    assert mid(None, None, 2.5) == 2.5
    assert mid(None, 3.0, None) == 3.0
    assert mid(4.0, None, None) == 4.0
    assert mid(None, None, None) is None
    assert mid(0, 0, 0) is None


def test_quote_side_bias():
    assert quote_side_bias(1.0, 1.2, 1.19) == "buyish"
    assert quote_side_bias(1.0, 1.2, 1.01) == "sellish"
    assert quote_side_bias(1.0, 1.2, 1.1) == "buyish"  # at/above mid
    assert quote_side_bias(None, 1.2, 1.1) == "unknown"
    assert quote_side_bias(1.2, 1.0, 1.1) == "unknown"  # crossed


def test_dte_uses_et_not_server_local():
    # Far-future expiry: must be positive and sane regardless of server tz.
    d = dte_from_expiry("2030-06-15")
    assert d is not None and d > 1000
    assert dte_from_expiry("not-a-date") is None
    # et_today matches America/New_York calendar date
    from zoneinfo import ZoneInfo
    from datetime import datetime

    assert et_today() == datetime.now(ZoneInfo("America/New_York")).date()
    assert isinstance(et_today(), date)


def test_with_retries_eventually_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    assert with_retries(flaky, attempts=3, base_delay=0.01) == "ok"
    assert calls["n"] == 3


def test_with_retries_reraises():
    with pytest.raises(ValueError):
        with_retries(lambda: (_ for _ in ()).throw(ValueError("x")), attempts=2, base_delay=0.01)
