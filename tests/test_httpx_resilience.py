"""Tests for timeout/TLS/DNS/reset/redirect-loop error clarity and the
retry-with-backoff behavior added to runtime/tools/httpx.py raw_request."""
from __future__ import annotations

import http.client
import socket
import ssl
import urllib.error
from io import BytesIO
from types import SimpleNamespace

from runtime.tools import httpx as http_impl
from runtime.tools.httpx import raw_request


def _ok_response(status=200, headers=None, body=b"ok"):
    from contextlib import contextmanager

    @contextmanager
    def fake_urlopen(request, timeout=20.0):
        resp = SimpleNamespace(
            status=status,
            headers=headers or {},
            read=lambda cap: body,
        )
        yield resp

    return fake_urlopen


class TestClearErrors:
    def test_timeout_error_names_url_and_timeout(self, monkeypatch):
        def fake_urlopen(*args, **kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request(
            "POST", "https://example.com/slow", timeout=7.5
        )
        assert status == 0
        assert err.startswith("error: fetch timed out")
        assert "https://example.com/slow" in err
        assert "7.5" in err

    def test_tls_cert_error_as_bare_exception(self, monkeypatch):
        def fake_urlopen(*args, **kwargs):
            raise ssl.SSLCertVerificationError("certificate verify failed: self-signed")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("POST", "https://example.com/")
        assert status == 0
        assert err.startswith("error: TLS certificate verification failed for example.com")

    def test_tls_cert_error_wrapped_in_urlerror(self, monkeypatch):
        def fake_urlopen(*args, **kwargs):
            raise urllib.error.URLError(
                ssl.SSLCertVerificationError("certificate verify failed: self-signed")
            )

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("POST", "https://example.com/")
        assert status == 0
        assert err.startswith("error: TLS certificate verification failed for example.com")

    def test_dns_failure_bare_exception(self, monkeypatch):
        def fake_urlopen(*args, **kwargs):
            raise socket.gaierror("nodename nor servname provided")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("POST", "https://bad.example/")
        assert status == 0
        assert err == "error: could not resolve host bad.example"

    def test_dns_failure_wrapped_in_urlerror(self, monkeypatch):
        def fake_urlopen(*args, **kwargs):
            raise urllib.error.URLError(socket.gaierror("not found"))

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("POST", "https://bad.example/")
        assert status == 0
        assert err == "error: could not resolve host bad.example"

    def test_connection_reset(self, monkeypatch):
        def fake_urlopen(*args, **kwargs):
            raise ConnectionResetError("connection reset by peer")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("POST", "https://example.com/")
        assert status == 0
        assert err == "error: connection reset by peer"

    def test_remote_disconnected(self, monkeypatch):
        def fake_urlopen(*args, **kwargs):
            raise http.client.RemoteDisconnected("Remote end closed connection")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("POST", "https://example.com/")
        assert status == 0
        assert err == "error: connection reset by peer"

    def test_redirect_loop_reported_clearly(self, monkeypatch):
        def fake_urlopen(*args, **kwargs):
            raise urllib.error.HTTPError(
                "https://example.com/loop",
                302,
                "The HTTP server returned a redirect error that would "
                "lead to an infinite loop.\nThe last 30x error message was:\nFound",
                {},
                BytesIO(b""),
            )

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("GET", "https://example.com/loop")
        assert status == 0
        assert err == "error: too many redirects for https://example.com/loop"


class TestRetryBehavior:
    def test_5xx_retried_then_returned_as_non_error(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(*args, **kwargs):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "https://example.com/", 503, "Service Unavailable", {}, BytesIO(b"down")
            )

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl.time, "sleep", lambda *_: None)
        status, hdrs, text, err = raw_request("GET", "https://example.com/")
        assert status == 503
        assert err == ""
        assert calls["n"] == http_impl.MAX_RETRIES + 1

    def test_5xx_succeeds_on_second_attempt(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    "https://example.com/", 500, "Server Error", {}, BytesIO(b"")
                )
            from contextlib import contextmanager

            @contextmanager
            def cm():
                yield SimpleNamespace(status=200, headers={}, read=lambda cap: b"ok")

            return cm()

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl.time, "sleep", lambda *_: None)
        status, hdrs, text, err = raw_request("GET", "https://example.com/")
        assert status == 200
        assert err == ""
        assert calls["n"] == 2

    def test_4xx_is_not_retried(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(*args, **kwargs):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "https://example.com/", 404, "Not Found", {}, BytesIO(b"nope")
            )

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl.time, "sleep", lambda *_: None)
        status, hdrs, text, err = raw_request("GET", "https://example.com/")
        assert status == 404
        assert err == ""
        assert calls["n"] == 1

    def test_post_not_retried_on_5xx(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(*args, **kwargs):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "https://example.com/", 500, "Server Error", {}, BytesIO(b"")
            )

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl.time, "sleep", lambda *_: None)
        status, hdrs, text, err = raw_request(
            "POST", "https://example.com/", body=b"x"
        )
        assert status == 500
        assert err == ""
        assert calls["n"] == 1

    def test_post_not_retried_on_transient_transport_failure(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(*args, **kwargs):
            calls["n"] += 1
            raise socket.gaierror("not found")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl.time, "sleep", lambda *_: None)
        status, hdrs, text, err = raw_request(
            "POST", "https://example.com/", body=b"x"
        )
        assert status == 0
        assert err == "error: could not resolve host example.com"
        assert calls["n"] == 1

    def test_get_retried_on_dns_failure_then_succeeds(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise socket.gaierror("not found")
            from contextlib import contextmanager

            @contextmanager
            def cm():
                yield SimpleNamespace(status=200, headers={}, read=lambda cap: b"ok")

            return cm()

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl.time, "sleep", lambda *_: None)
        status, hdrs, text, err = raw_request("GET", "https://example.com/")
        assert status == 200
        assert err == ""
        assert calls["n"] == 2

    def test_tls_error_not_retried(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(*args, **kwargs):
            calls["n"] += 1
            raise ssl.SSLCertVerificationError("bad cert")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl.time, "sleep", lambda *_: None)
        status, hdrs, text, err = raw_request("GET", "https://example.com/")
        assert status == 0
        assert calls["n"] == 1
        assert err.startswith("error: TLS certificate verification failed")

    def test_timeout_retried_up_to_cap_then_clear_error(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(*args, **kwargs):
            calls["n"] += 1
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl.time, "sleep", lambda *_: None)
        status, hdrs, text, err = raw_request("GET", "https://example.com/", timeout=3)
        assert status == 0
        assert calls["n"] == http_impl.MAX_RETRIES + 1
        assert err.startswith("error: fetch timed out")
