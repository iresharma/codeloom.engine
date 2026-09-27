"""Tests for the retry/TLS/DNS/redirect hardening in runtime/tools/httpx.py."""
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


def _ok_response(body: bytes = b"ok"):
    @contextmanager
    def fake_urlopen(request, timeout=20.0):
        resp = SimpleNamespace(
            status=200,
            headers={},
            read=lambda cap: body,
        )
        yield resp

    return fake_urlopen


class TestRetryTransient:
    def test_retry_succeeds_after_two_timeouts(self, monkeypatch):
        calls = []

        @contextmanager
        def flaky_urlopen(request, timeout=20.0):
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError("timed out")
            resp = SimpleNamespace(status=200, headers={}, read=lambda cap: b"hi")
            yield resp

        monkeypatch.setattr(http_impl, "urlopen", flaky_urlopen)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert err == ""
        assert status == 200
        assert text == "hi"
        assert len(calls) == 3

    def test_retry_exhausted_timeout(self, monkeypatch):
        calls = []

        def always_timeout(*args, **kwargs):
            calls.append(1)
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", always_timeout)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == http_impl.MAX_RETRIES
        assert err.startswith("error:")
        assert "attempt" in err.lower()
        assert "timed out" in err.lower()

    def test_post_never_retried(self, monkeypatch):
        calls = []

        def always_timeout(*args, **kwargs):
            calls.append(1)
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", always_timeout)
        status, hdrs, text, err = raw_request("POST", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert err.startswith("error:")

    def test_patch_never_retried(self, monkeypatch):
        calls = []

        def always_timeout(*args, **kwargs):
            calls.append(1)
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", always_timeout)
        status, hdrs, text, err = raw_request("PATCH", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert err.startswith("error:")


class TestRetryStatusCodes:
    def test_5xx_retried_then_exhausted_returns_real_body(self, monkeypatch):
        calls = []

        def always_500(*args, **kwargs):
            calls.append(1)
            raise urllib.error.HTTPError(
                "https://example.com", 503, "Service Unavailable", {}, BytesIO(b"down")
            )

        monkeypatch.setattr(http_impl, "urlopen", always_500)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert len(calls) == http_impl.MAX_RETRIES
        assert err == ""
        assert status == 503
        assert text == "down"

    def test_4xx_never_retried(self, monkeypatch):
        calls = []

        def always_404(*args, **kwargs):
            calls.append(1)
            raise urllib.error.HTTPError(
                "https://example.com", 404, "Not Found", {}, BytesIO(b"nope")
            )

        monkeypatch.setattr(http_impl, "urlopen", always_404)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert len(calls) == 1
        assert err == ""
        assert status == 404


class TestTLSErrors:
    def test_cert_verification_error(self, monkeypatch):
        calls = []

        def raise_cert_error(*args, **kwargs):
            calls.append(1)
            raise ssl.SSLCertVerificationError("certificate verify failed")

        monkeypatch.setattr(http_impl, "urlopen", raise_cert_error)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert err.startswith("error:")
        assert "certificate" in err.lower() or "tls" in err.lower()

    def test_cert_verification_error_wrapped_in_urlerror(self, monkeypatch):
        calls = []

        def raise_wrapped(*args, **kwargs):
            calls.append(1)
            raise urllib.error.URLError(ssl.SSLCertVerificationError("bad cert"))

        monkeypatch.setattr(http_impl, "urlopen", raise_wrapped)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert "certificate" in err.lower() or "tls" in err.lower()

    def test_generic_ssl_error_not_retried(self, monkeypatch):
        calls = []

        def raise_ssl_error(*args, **kwargs):
            calls.append(1)
            raise ssl.SSLError("handshake failure")

        monkeypatch.setattr(http_impl, "urlopen", raise_ssl_error)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert err.startswith("error:")


class TestDNSErrors:
    def test_dns_failure_wrapped_in_urlerror(self, monkeypatch):
        calls = []

        def raise_dns(*args, **kwargs):
            calls.append(1)
            raise urllib.error.URLError(socket.gaierror("Name or service not known"))

        monkeypatch.setattr(http_impl, "urlopen", raise_dns)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == http_impl.MAX_RETRIES
        assert err.startswith("error:")
        assert "dns" in err.lower() or "resolution" in err.lower()

    def test_dns_failure_bare_gaierror(self, monkeypatch):
        calls = []

        def raise_dns(*args, **kwargs):
            calls.append(1)
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(http_impl, "urlopen", raise_dns)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == http_impl.MAX_RETRIES
        assert "dns" in err.lower() or "resolution" in err.lower()


class TestConnectionReset:
    def test_connection_reset_retried_for_get(self, monkeypatch):
        calls = []

        def raise_reset(*args, **kwargs):
            calls.append(1)
            raise ConnectionResetError("Connection reset by peer")

        monkeypatch.setattr(http_impl, "urlopen", raise_reset)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == http_impl.MAX_RETRIES
        assert err.startswith("error:")
        assert "reset" in err.lower()

    def test_connection_reset_not_retried_for_post(self, monkeypatch):
        calls = []

        def raise_reset(*args, **kwargs):
            calls.append(1)
            raise ConnectionResetError("Connection reset by peer")

        monkeypatch.setattr(http_impl, "urlopen", raise_reset)
        status, hdrs, text, err = raw_request("POST", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert "reset" in err.lower()


class TestRedirectLoop:
    def test_too_many_redirects_310(self, monkeypatch):
        calls = []

        def raise_redirect_loop(*args, **kwargs):
            calls.append(1)
            raise urllib.error.HTTPError(
                "https://example.com", 310, "Too many redirects", {}, None
            )

        monkeypatch.setattr(http_impl, "urlopen", raise_redirect_loop)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert err.startswith("error:")
        assert "redirect" in err.lower()

    def test_too_many_redirects_urllib_style_message(self, monkeypatch):
        """urllib actually raises the original redirect status code with an
        infinite-loop message rather than a dedicated status code."""
        calls = []
        msg = (
            "The HTTP server returned a redirect error that would lead to "
            "an infinite loop.\nThe last 30x error message was:\nFound"
        )

        def raise_redirect_loop(*args, **kwargs):
            calls.append(1)
            raise urllib.error.HTTPError(
                "https://example.com", 302, msg, {}, None
            )

        monkeypatch.setattr(http_impl, "urlopen", raise_redirect_loop)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert len(calls) == 1
        assert err.startswith("error:")
        assert "redirect" in err.lower()


class TestMaxRedirectsConstant:
    def test_safe_redirect_uses_named_constant(self):
        assert http_impl._SafeRedirect.max_redirections == http_impl.MAX_REDIRECTS
