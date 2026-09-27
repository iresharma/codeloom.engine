"""Additional edge case tests for runtime/tools/httpx.py hardening."""
from __future__ import annotations

import socket
import ssl
import urllib.error
from io import BytesIO
from types import SimpleNamespace
from contextlib import contextmanager

import pytest

from runtime.tools import httpx as http_impl
from runtime.tools.httpx import raw_request


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Never actually wait for backoff in tests."""
    monkeypatch.setattr(http_impl.time, "sleep", lambda *_a, **_k: None)


class TestIdempotentMethodRetry:
    """Test that PUT and DELETE are retried as idempotent methods."""

    def test_put_retried_on_timeout(self, monkeypatch):
        """PUT is idempotent: should be retried on timeout."""
        calls = []

        def always_timeout(*args, **kwargs):
            calls.append(1)
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", always_timeout)
        status, hdrs, text, err = raw_request("PUT", "https://example.com")
        assert status == 0
        assert len(calls) == http_impl.MAX_RETRIES
        assert err.startswith("error:")

    def test_delete_retried_on_timeout(self, monkeypatch):
        """DELETE is idempotent: should be retried on timeout."""
        calls = []

        def always_timeout(*args, **kwargs):
            calls.append(1)
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", always_timeout)
        status, hdrs, text, err = raw_request("DELETE", "https://example.com")
        assert status == 0
        assert len(calls) == http_impl.MAX_RETRIES
        assert err.startswith("error:")

    def test_put_retried_on_connection_reset(self, monkeypatch):
        """PUT is idempotent: should be retried on connection reset."""
        calls = []

        def always_reset(*args, **kwargs):
            calls.append(1)
            raise ConnectionResetError("Connection reset by peer")

        monkeypatch.setattr(http_impl, "urlopen", always_reset)
        status, hdrs, text, err = raw_request("PUT", "https://example.com")
        assert status == 0
        assert len(calls) == http_impl.MAX_RETRIES
        assert "reset" in err.lower()

    def test_delete_retried_on_dns_failure(self, monkeypatch):
        """DELETE is idempotent: should be retried on DNS failure."""
        calls = []

        def raise_dns(*args, **kwargs):
            calls.append(1)
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(http_impl, "urlopen", raise_dns)
        status, hdrs, text, err = raw_request("DELETE", "https://example.com")
        assert status == 0
        assert len(calls) == http_impl.MAX_RETRIES
        assert "dns" in err.lower() or "resolution" in err.lower()


class TestNonIdempotentNoRetry:
    """Test that non-idempotent methods (POST, PATCH) are never retried."""

    def test_post_not_retried_on_connection_reset(self, monkeypatch):
        """POST is non-idempotent: should NOT be retried on connection reset."""
        calls = []

        def always_reset(*args, **kwargs):
            calls.append(1)
            raise ConnectionResetError("Connection reset by peer")

        monkeypatch.setattr(http_impl, "urlopen", always_reset)
        status, hdrs, text, err = raw_request("POST", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert "reset" in err.lower()

    def test_patch_not_retried_on_dns_failure(self, monkeypatch):
        """PATCH is non-idempotent: should NOT be retried on DNS failure."""
        calls = []

        def raise_dns(*args, **kwargs):
            calls.append(1)
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(http_impl, "urlopen", raise_dns)
        status, hdrs, text, err = raw_request("PATCH", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert "dns" in err.lower() or "resolution" in err.lower()


class TestRetryAttemptMessages:
    """Test that retry exhaustion messages are clear and informative."""

    def test_timeout_exhaustion_mentions_attempts(self, monkeypatch):
        """After retry exhaustion, timeout message should mention number of attempts."""
        calls = []

        def always_timeout(*args, **kwargs):
            calls.append(1)
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", always_timeout)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == http_impl.MAX_RETRIES
        assert "attempt" in err.lower()
        assert str(http_impl.MAX_RETRIES) in err

    def test_connection_reset_exhaustion_mentions_attempts(self, monkeypatch):
        """After retry exhaustion, connection error should mention number of attempts."""
        calls = []

        def always_reset(*args, **kwargs):
            calls.append(1)
            raise ConnectionResetError("Connection reset by peer")

        monkeypatch.setattr(http_impl, "urlopen", always_reset)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == http_impl.MAX_RETRIES
        assert "attempt" in err.lower()
        # Error message should reference the URL
        assert "example.com" in err.lower()


class TestTLSErrorNoRetry:
    """Test that TLS/certificate errors are never retried."""

    def test_cert_error_not_retried_for_get(self, monkeypatch):
        """TLS cert errors should NOT be retried even for idempotent methods."""
        calls = []

        def raise_cert_error(*args, **kwargs):
            calls.append(1)
            raise ssl.SSLCertVerificationError("certificate verify failed")

        monkeypatch.setattr(http_impl, "urlopen", raise_cert_error)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert "certificate" in err.lower() or "tls" in err.lower()

    def test_ssl_error_not_retried_for_get(self, monkeypatch):
        """Generic SSL errors should NOT be retried even for idempotent methods."""
        calls = []

        def raise_ssl_error(*args, **kwargs):
            calls.append(1)
            raise ssl.SSLError("handshake failure")

        monkeypatch.setattr(http_impl, "urlopen", raise_ssl_error)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert "tls" in err.lower() or "ssl" in err.lower()


class TestFourxxNoRetry:
    """Test that 4xx responses are never retried even if method is idempotent."""

    def test_404_not_retried_for_get(self, monkeypatch):
        """4xx errors should NOT be retried."""
        calls = []

        def raise_404(*args, **kwargs):
            calls.append(1)
            raise urllib.error.HTTPError(
                "https://example.com", 404, "Not Found", {}, BytesIO(b"not found")
            )

        monkeypatch.setattr(http_impl, "urlopen", raise_404)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 404
        assert err == ""
        assert len(calls) == 1

    def test_401_not_retried_for_get(self, monkeypatch):
        """4xx errors should NOT be retried."""
        calls = []

        def raise_401(*args, **kwargs):
            calls.append(1)
            raise urllib.error.HTTPError(
                "https://example.com", 401, "Unauthorized", {}, BytesIO(b"auth required")
            )

        monkeypatch.setattr(http_impl, "urlopen", raise_401)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 401
        assert err == ""
        assert len(calls) == 1


class TestFirstAttemptNoDelay:
    """Test that first attempt never waits for backoff."""

    def test_timeout_first_attempt_immediate(self, monkeypatch):
        """First timeout should fail immediately without sleep."""
        sleep_calls = []

        def track_sleep(seconds):
            sleep_calls.append(seconds)

        def always_timeout(*args, **kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl.time, "sleep", track_sleep)
        monkeypatch.setattr(http_impl, "urlopen", always_timeout)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        # Should have tried MAX_RETRIES times, with (MAX_RETRIES - 1) sleeps
        assert len(sleep_calls) == http_impl.MAX_RETRIES - 1
        # First sleep should be RETRY_BACKOFF_BASE * 2^0 = 0.5
        assert sleep_calls[0] == http_impl.RETRY_BACKOFF_BASE * (2**0)


class TestBackoffExponential:
    """Test that backoff grows exponentially."""

    def test_backoff_exponential_growth(self, monkeypatch):
        """Each retry should have exponentially increasing backoff."""
        sleep_calls = []

        def track_sleep(seconds):
            sleep_calls.append(seconds)

        def always_timeout(*args, **kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl.time, "sleep", track_sleep)
        monkeypatch.setattr(http_impl, "urlopen", always_timeout)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        # Verify exponential backoff: 0.5, 1.0, 2.0, ...
        for i, sleep_duration in enumerate(sleep_calls):
            expected = http_impl.RETRY_BACKOFF_BASE * (2**i)
            assert sleep_duration == expected


class TestURLErrorGenericRetry:
    """Test that generic URLErrors (not SSL/DNS) are retried."""

    def test_generic_urlerror_retried_for_get(self, monkeypatch):
        """Generic URLError should be retried for idempotent methods."""
        calls = []

        def raise_urlerror(*args, **kwargs):
            calls.append(1)
            raise urllib.error.URLError("network unreachable")

        monkeypatch.setattr(http_impl, "urlopen", raise_urlerror)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == http_impl.MAX_RETRIES
        assert "network unreachable" in err.lower()

    def test_generic_urlerror_not_retried_for_post(self, monkeypatch):
        """Generic URLError should NOT be retried for non-idempotent methods."""
        calls = []

        def raise_urlerror(*args, **kwargs):
            calls.append(1)
            raise urllib.error.URLError("network unreachable")

        monkeypatch.setattr(http_impl, "urlopen", raise_urlerror)
        status, hdrs, text, err = raw_request("POST", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert "network unreachable" in err.lower()
