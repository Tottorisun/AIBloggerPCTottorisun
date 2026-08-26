"""Regression coverage for BaseSource's robots.txt / WAF-detection plumbing.

test_transport_error_during_robots_fetch_does_not_crash reproduces a real
bug hit during a live scrape: robots.txt fetch failing at the transport
level (connection error, timeout — not just a bad HTTP status) raised
UnboundLocalError instead of the intended SourceBlocked, because `waf` was
only assigned after a response existed.
"""

from datetime import datetime

import httpx
import pytest

from pc_price_tracker.sources.base import BaseSource, SourceBlocked


class _DummySource(BaseSource):
    name = "dummy"
    base_url = "https://example.invalid"

    def fetch_category(self, category, captured_at):
        raise NotImplementedError


class _RaisingClient:
    """Minimal stand-in for httpx.Client whose .get() always fails at the
    transport level, before any httpx.Response exists."""

    def get(self, url, **kwargs):
        raise httpx.ConnectError("simulated connection failure", request=httpx.Request("GET", url))


def test_transport_error_during_robots_fetch_does_not_crash():
    source = _DummySource(client=_RaisingClient())
    with pytest.raises(SourceBlocked):
        source._check_allowed("https://example.invalid/catalog/gpu")


class _WafClient:
    """Returns a 429 with a known WAF vendor header — should fail closed
    with a WAF-attributed reason, not just "robots.txt disallows"."""

    def get(self, url, **kwargs):
        return httpx.Response(429, headers={"server": "qrator"}, request=httpx.Request("GET", url))


def test_waf_response_during_robots_fetch_is_attributed():
    source = _DummySource(client=_WafClient())
    with pytest.raises(SourceBlocked, match="qrator"):
        source._check_allowed("https://example.invalid/catalog/gpu")
