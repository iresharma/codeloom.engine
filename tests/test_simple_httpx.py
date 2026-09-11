"""Simple test to verify pytest works."""
from __future__ import annotations

from runtime.tools import httpx


def test_simple_hostname():
    """Test basic hostname extraction."""
    assert httpx._hostname("example.com") == "example.com"
    assert httpx._hostname("example.com:8080") == "example.com"
    assert httpx._hostname("localhost:8000") == "localhost"
    assert httpx._hostname("[::1]") == "::1"
    assert httpx._hostname("user@example.com") == "example.com"
