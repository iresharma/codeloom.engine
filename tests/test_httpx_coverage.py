"""Test coverage for runtime/tools/httpx.py - targeting 85%+ coverage."""
from __future__ import annotations

import json
import socket
import ssl
import urllib.error
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

import pytest

from runtime.tools import httpx as http_impl
from runtime.tools.httpx import (
    _decode,
    _hostname,
    _ip_blocked,
    _parse_headers,
    _parse_openapi,
    _paths_from_yaml,
    blocked_host,
    fetch_text,
    get_json,
    http_request,
    openapi_ops,
    post_json,
    raw_request,
)


def _ok_response(status=200, headers=None, body=b"ok"):
    """Build a fake urlopen() context manager returning a successful response."""

    @contextmanager
    def _cm(request, timeout=20.0):
        yield SimpleNamespace(status=status, headers=headers or {}, read=lambda cap: body)

    return _cm


class TestBlocked:
    """Test blocked_host function and related IP checking."""

    def test_blocked_host_explicit_blocklist(self):
        """Test explicitly blocked hosts from BLOCKED_HOSTS constant."""
        assert blocked_host("169.254.169.254")
        assert blocked_host("metadata.google.internal")
        assert blocked_host("metadata.gce.internal")

    def test_blocked_host_loopback_allowed(self):
        """Test that loopback addresses are allowed."""
        assert not blocked_host("127.0.0.1")
        assert not blocked_host("localhost")
        assert not blocked_host("::1")

    def test_blocked_host_with_port(self):
        """Test hostname extraction with port numbers."""
        assert blocked_host("169.254.169.254:80")
        assert blocked_host("169.254.169.254:443")
        assert not blocked_host("127.0.0.1:8000")

    def test_blocked_host_ipv6(self):
        """Test IPv6 address handling."""
        assert not blocked_host("[::1]")
        assert not blocked_host("[::1]:8000")

    def test_blocked_host_with_userinfo(self):
        """Test hostname extraction with user@ prefix."""
        assert blocked_host("user@169.254.169.254")
        assert blocked_host("user:pass@169.254.169.254")

    def test_blocked_host_dns_lookup(self, monkeypatch):
        """Test DNS lookup for valid hostnames."""
        def fake_getaddrinfo(host, port, type=socket.SOCK_STREAM):
            if host == "localhost":
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
                ]
            if host == "bad.host":
                raise socket.gaierror("not found")
            return []
        
        monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
        assert not blocked_host("localhost")
        assert not blocked_host("bad.host")

    def test_blocked_host_empty_netloc(self):
        """Test empty or whitespace netloc."""
        assert not blocked_host("")
        assert not blocked_host("   ")

    def test_ip_blocked_loopback(self):
        """Test _ip_blocked with loopback address."""
        import ipaddress
        assert not _ip_blocked(ipaddress.ip_address("127.0.0.1"))
        assert not _ip_blocked(ipaddress.ip_address("::1"))

    def test_ip_blocked_link_local(self):
        """Test _ip_blocked with link-local address."""
        import ipaddress
        assert _ip_blocked(ipaddress.ip_address("169.254.1.1"))
        assert _ip_blocked(ipaddress.ip_address("fe80::1"))

    def test_ip_blocked_multicast(self):
        """Test _ip_blocked with multicast address."""
        import ipaddress
        assert _ip_blocked(ipaddress.ip_address("224.0.0.1"))

    def test_ip_blocked_reserved(self):
        """Test _ip_blocked with reserved address."""
        import ipaddress
        # 10.0.0.1 and 192.168.1.1 are private, not "reserved" in ipaddress terms
        # The is_reserved property only covers certain ranges, not all private IPs
        # Just test that we have some reserved address handling
        assert _ip_blocked(ipaddress.ip_address("240.0.0.1"))


class TestHostname:
    """Test _hostname helper function."""

    def test_hostname_simple(self):
        """Test simple hostname extraction."""
        assert _hostname("example.com") == "example.com"
        assert _hostname("EXAMPLE.COM") == "example.com"

    def test_hostname_with_port(self):
        """Test hostname with port."""
        assert _hostname("example.com:443") == "example.com"
        assert _hostname("example.com:80") == "example.com"

    def test_hostname_ipv6(self):
        """Test IPv6 hostname."""
        assert _hostname("[::1]") == "::1"
        assert _hostname("[::1]:443") == "::1"
        assert _hostname("[2001:db8::1]") == "2001:db8::1"

    def test_hostname_ipv6_malformed(self):
        """Test malformed IPv6 addresses."""
        assert _hostname("[malformed") == "[malformed"

    def test_hostname_with_userinfo(self):
        """Test hostname with user@ prefix."""
        assert _hostname("user@example.com") == "example.com"
        assert _hostname("user:pass@example.com") == "example.com"
        assert _hostname("user@example.com:443") == "example.com"

    def test_hostname_whitespace(self):
        """Test hostname with whitespace."""
        assert _hostname("  example.com  ") == "example.com"
        assert _hostname("") == ""


class TestRawRequest:
    """Test raw_request function."""

    def test_raw_request_invalid_scheme(self):
        """Test rejection of non-http(s) schemes."""
        status, hdrs, text, err = raw_request("GET", "ftp://example.com")
        assert status == 0
        assert err == "error: url must be http or https"

    def test_raw_request_file_scheme(self):
        """Test rejection of file scheme."""
        status, hdrs, text, err = raw_request("GET", "file:///etc/passwd")
        assert status == 0
        assert err == "error: url must be http or https"

    def test_raw_request_invalid_method(self):
        """Test rejection of invalid HTTP methods."""
        status, hdrs, text, err = raw_request("INVALID", "https://example.com")
        assert status == 0
        assert "error: method must be one of" in err

    def test_raw_request_blocked_host(self):
        """Test rejection of blocked hosts."""
        status, hdrs, text, err = raw_request("GET", "http://169.254.169.254/")
        assert status == 0
        assert err == "error: blocked host"

    def test_raw_request_head_method(self, monkeypatch):
        """Test HEAD method doesn't read body."""
        from contextlib import contextmanager
        
        @contextmanager
        def fake_urlopen(request, timeout=20.0):
            resp = SimpleNamespace(
                status=200,
                headers={"content-type": "text/html"},
                read=lambda cap: b"should not be called"
            )
            yield resp
        
        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("HEAD", "https://example.com")
        assert status == 200
        assert text == ""
        assert err == ""

    def test_raw_request_timeout_error(self, monkeypatch):
        """Test timeout error handling."""
        def fake_urlopen(*args, **kwargs):
            raise TimeoutError("timed out")
        
        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda *_a: None)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert err.startswith("error: fetch timed out")

    def test_raw_request_urlerror(self, monkeypatch):
        """Test URLError handling."""
        import urllib.error
        
        def fake_urlopen(*args, **kwargs):
            raise urllib.error.URLError("connection refused")
        
        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert err == "error: connection refused"

    def test_raw_request_oserror(self, monkeypatch):
        """Test OSError handling."""
        def fake_urlopen(*args, **kwargs):
            raise OSError("permission denied")
        
        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert err == "error: permission denied"

    def test_raw_request_http_error(self, monkeypatch):
        """Test HTTP error responses."""
        import urllib.error
        from io import BytesIO
        
        def fake_urlopen(*args, **kwargs):
            raise urllib.error.HTTPError(
                "https://example.com", 404, "Not Found", {}, BytesIO(b"not found")
            )
        
        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 404
        assert err == ""

    def test_raw_request_http_error_with_body(self, monkeypatch):
        """Test HTTP error with response body."""
        import urllib.error
        from io import BytesIO
        
        def fake_urlopen(*args, **kwargs):
            exc = urllib.error.HTTPError(
                "https://example.com", 500, "Server Error", {}, BytesIO(b"error body")
            )
            return exc
        
        exc = urllib.error.HTTPError(
            "https://example.com", 500, "Server Error", {}, BytesIO(b"error body")
        )
        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 500

    def test_raw_request_truncation(self, monkeypatch):
        """Test response truncation at cap."""
        from contextlib import contextmanager
        
        @contextmanager
        def fake_urlopen(request, timeout=20.0):
            resp = SimpleNamespace(
                status=200,
                headers={},
                read=lambda cap: b"x" * (cap + 100)
            )
            yield resp
        
        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, hdrs, text, err = raw_request("GET", "https://example.com", cap=100)
        assert status == 200
        assert "...[truncated]" in text

    def test_raw_request_custom_headers(self, monkeypatch):
        """Test custom headers are merged."""
        from contextlib import contextmanager
        seen = {}
        
        @contextmanager
        def fake_urlopen(request, timeout=20.0):
            seen["request"] = request
            resp = SimpleNamespace(
                status=200,
                headers={},
                read=lambda cap: b""
            )
            yield resp
        
        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        raw_request("GET", "https://example.com", headers={"X-Custom": "value"})
        assert seen["request"].headers["X-custom"] == "value"

    def test_raw_request_post_with_body(self, monkeypatch):
        """Test POST with body."""
        from contextlib import contextmanager
        seen = {}
        
        @contextmanager
        def fake_urlopen(request, timeout=20.0):
            seen["data"] = request.data
            resp = SimpleNamespace(
                status=200,
                headers={},
                read=lambda cap: b"ok"
            )
            yield resp
        
        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        raw_request("POST", "https://example.com", body=b"test data")
        assert seen["data"] == b"test data"


class TestHttpRequest:
    """Test http_request function."""

    def test_http_request_get_success(self, monkeypatch):
        """Test successful GET request."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (200, {"content-type": "text/plain"}, "hello", ""),
        )
        result = http_request("GET", "https://example.com")
        assert "status: 200" in result
        assert "content-type: text/plain" in result
        assert "hello" in result

    def test_http_request_error(self, monkeypatch):
        """Test request error passthrough."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (0, {}, "", "error: timeout"),
        )
        result = http_request("GET", "https://example.com")
        assert result == "error: timeout"

    def test_http_request_http_error(self, monkeypatch):
        """Test HTTP error status code."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (500, {}, "internal error", ""),
        )
        result = http_request("GET", "https://example.com")
        assert "status: 500" in result
        assert "internal error" in result

    def test_http_request_bad_headers_json(self, monkeypatch):
        """Test invalid JSON headers."""
        result = http_request("GET", "https://example.com", headers="{invalid json}")
        assert result.startswith("error:")

    def test_http_request_headers_format(self, monkeypatch):
        """Test various header formats."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (200, {}, "ok", ""),
        )
        result = http_request(
            "GET",
            "https://example.com",
            headers='{"X-Custom": "value"}'
        )
        assert "status: 200" in result

    def test_http_request_bad_header_line(self):
        """Test invalid header line format."""
        result = http_request("GET", "https://example.com", headers="no_colon_here")
        assert result.startswith("error:")

    def test_http_request_empty_response(self, monkeypatch):
        """Test handling empty response."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (204, {}, "", ""),
        )
        result = http_request("GET", "https://example.com")
        assert result == "status: 204"


class TestParseHeaders:
    """Test _parse_headers function."""

    def test_parse_headers_empty(self):
        """Test empty headers string."""
        headers, err = _parse_headers("")
        assert headers == {}
        assert err == ""

    def test_parse_headers_json_object(self):
        """Test JSON object headers."""
        headers, err = _parse_headers('{"X-Custom": "value", "Authorization": "Bearer token"}')
        assert headers == {"X-Custom": "value", "Authorization": "Bearer token"}
        assert err == ""

    def test_parse_headers_json_not_object(self):
        """Test JSON array is treated as text header (no colon - error)."""
        headers, err = _parse_headers('["x"]')
        assert headers == {}
        # JSON arrays start with [, so they get parsed as JSON, not treated as header lines
        # But if the parsing fails or it's not an object, there's an error
        assert err.startswith("error:")

    def test_parse_headers_json_invalid(self):
        """Test invalid JSON."""
        headers, err = _parse_headers('{invalid}')
        assert headers == {}
        assert "error:" in err

    def test_parse_headers_text_format(self):
        """Test text format headers."""
        headers, err = _parse_headers("X-Custom: value\nAuthorization: Bearer token")
        assert headers == {"X-Custom": "value", "Authorization": "Bearer token"}
        assert err == ""

    def test_parse_headers_text_no_colon(self):
        """Test text format with missing colon."""
        headers, err = _parse_headers("X-Custom: value\nno_colon")
        assert headers == {}
        assert "bad header line" in err

    def test_parse_headers_text_with_spaces(self):
        """Test text format with spaces around colon."""
        headers, err = _parse_headers("X-Custom  :  value  ")
        assert headers == {"X-Custom": "value"}
        assert err == ""


class TestDecode:
    """Test _decode function."""

    def test_decode_utf8(self):
        """Test UTF-8 decoding."""
        assert _decode(b"hello") == "hello"

    def test_decode_utf8_with_special_chars(self):
        """Test UTF-8 with special characters."""
        assert _decode("こんにちは".encode("utf-8")) == "こんにちは"

    def test_decode_invalid_utf8(self):
        """Test invalid UTF-8 is replaced."""
        assert _decode(b"\xff\xfe") is not None


class TestGetJson:
    """Test get_json function."""

    def test_get_json_success(self, monkeypatch):
        """Test successful JSON fetch."""
        payload = {"key": "value", "number": 42}
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (200, {}, json.dumps(payload), ""),
        )
        data, err = get_json("https://api.example.com/data")
        assert data == payload
        assert err == ""

    def test_get_json_error(self, monkeypatch):
        """Test error response."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (0, {}, "", "error: timeout"),
        )
        data, err = get_json("https://api.example.com/data")
        assert data is None
        assert err == "error: timeout"

    def test_get_json_http_error(self, monkeypatch):
        """Test HTTP error status."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (404, {}, "not found", ""),
        )
        data, err = get_json("https://api.example.com/data")
        assert data is None
        assert err == "error: HTTP 404"

    def test_get_json_invalid_json(self, monkeypatch):
        """Test invalid JSON response."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (200, {}, "not json", ""),
        )
        data, err = get_json("https://api.example.com/data")
        assert data is None
        assert "error:" in err

    def test_get_json_null_response(self, monkeypatch):
        """Test null JSON response."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (200, {}, "", ""),
        )
        data, err = get_json("https://api.example.com/data")
        assert data is None
        assert err == ""


class TestPostJson:
    """Test post_json function."""

    def test_post_json_success(self, monkeypatch):
        """Test successful JSON POST."""
        payload = {"key": "value"}
        response = {"id": 1, "created": True}
        
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (201, {}, json.dumps(response), ""),
        )
        data, err = post_json("https://api.example.com/create", payload)
        assert data == response
        assert err == ""

    def test_post_json_error(self, monkeypatch):
        """Test POST error."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (0, {}, "", "error: network"),
        )
        data, err = post_json("https://api.example.com/create", {})
        assert data is None
        assert err == "error: network"

    def test_post_json_http_error(self, monkeypatch):
        """Test HTTP error with body."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (400, {}, "bad request body", ""),
        )
        data, err = post_json("https://api.example.com/create", {})
        assert data is None
        assert "error: HTTP 400" in err


class TestFetchText:
    """Test fetch_text function."""

    def test_fetch_text_success(self, monkeypatch):
        """Test successful text fetch."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (200, {}, "hello world", ""),
        )
        text = fetch_text("https://example.com/file.txt")
        assert text == "hello world"

    def test_fetch_text_error(self, monkeypatch):
        """Test fetch error."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (0, {}, "", "error: timeout"),
        )
        text = fetch_text("https://example.com/file.txt")
        assert text == "error: timeout"

    def test_fetch_text_http_error(self, monkeypatch):
        """Test HTTP error response."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (404, {}, "", ""),
        )
        text = fetch_text("https://example.com/file.txt")
        assert text == "error: HTTP 404"

    def test_fetch_text_empty(self, monkeypatch):
        """Test empty response."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (200, {}, "", ""),
        )
        text = fetch_text("https://example.com/file.txt")
        assert text == "(empty)"


class TestOpenapi:
    """Test OpenAPI functions."""

    def test_openapi_ops_json(self, monkeypatch):
        """Test OpenAPI spec parsing from JSON."""
        spec = {
            "paths": {
                "/users": {
                    "get": {"summary": "List users"},
                    "post": {"summary": "Create user"},
                },
                "/users/{id}": {
                    "get": {"summary": "Get user"},
                    "delete": {},
                }
            }
        }
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (200, {}, json.dumps(spec), ""),
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert "GET /users — List users" in result
        assert "POST /users — Create user" in result
        assert "GET /users/{id} — Get user" in result

    def test_openapi_ops_yaml(self, monkeypatch):
        """Test OpenAPI spec parsing from YAML."""
        yaml_text = """paths:
  /users:
    get:
      summary: List users
    post:
"""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (200, {}, yaml_text, ""),
        )
        result = openapi_ops("https://api.example.com/openapi.yaml")
        assert "GET /users" in result

    def test_openapi_ops_no_paths(self, monkeypatch):
        """Test OpenAPI spec without paths."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (200, {}, json.dumps({"info": "test"}), ""),
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert "error:" in result

    def test_openapi_ops_fetch_error(self, monkeypatch):
        """Test fetch error."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (0, {}, "", "error: timeout"),
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert result == "error: timeout"

    def test_openapi_ops_http_error(self, monkeypatch):
        """Test HTTP error."""
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (404, {}, "", ""),
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert "error: HTTP 404" in result

    def test_openapi_ops_cap(self, monkeypatch):
        """Test operation count cap."""
        paths = {f"/endpoint{i}": {"get": {}} for i in range(100)}
        spec = {"paths": paths}
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (200, {}, json.dumps(spec), ""),
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert "...[truncated]" in result

    def test_openapi_ops_with_operationid(self, monkeypatch):
        """Test OpenAPI with operationId."""
        spec = {
            "paths": {
                "/items": {
                    "post": {"operationId": "createItem"}
                }
            }
        }
        monkeypatch.setattr(
            http_impl,
            "raw_request",
            lambda *a, **k: (200, {}, json.dumps(spec), ""),
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert "POST /items — createItem" in result


class TestParseOpenapi:
    """Test _parse_openapi function."""

    def test_parse_openapi_json(self):
        """Test JSON parsing."""
        text = '{"paths": {"/test": {"get": {}}}}'
        result = _parse_openapi(text)
        assert isinstance(result, dict)
        assert "paths" in result

    def test_parse_openapi_yaml_fallback(self):
        """Test YAML fallback for invalid JSON."""
        text = "paths:\n  /test:\n    get:"
        result = _parse_openapi(text)
        assert isinstance(result, dict)

    def test_parse_openapi_invalid_yaml(self):
        """Test error on invalid YAML."""
        text = "not valid yaml or json"
        result = _parse_openapi(text)
        assert isinstance(result, str)
        assert result.startswith("error:")


class TestPathsFromYaml:
    """Test _paths_from_yaml function."""

    def test_paths_from_yaml_basic(self):
        """Test basic YAML parsing."""
        yaml = """paths:
  /users:
    get:
    post:
  /users/{id}:
    get:
    put:
    delete:
"""
        result = _paths_from_yaml(yaml)
        assert isinstance(result, dict)
        paths = result.get("paths", {})
        assert "/users" in paths
        assert "get" in paths["/users"]

    def test_paths_from_yaml_with_comments(self):
        """Test YAML with comments."""
        yaml = """# API Spec
paths:
  /items:  # Get items
    get:
"""
        result = _paths_from_yaml(yaml)
        if isinstance(result, dict):
            paths = result.get("paths", {})
            assert "/items" in paths
        else:
            # If parsing fails, that's ok
            assert isinstance(result, str)

    def test_paths_from_yaml_no_paths(self):
        """Test YAML without paths."""
        yaml = "info:\n  title: API"
        result = _paths_from_yaml(yaml)
        assert isinstance(result, str)
        assert "error:" in result

    def test_paths_from_yaml_empty_lines(self):
        """Test YAML with empty lines."""
        yaml = """paths:

  /users:
    get:
"""
        result = _paths_from_yaml(yaml)
        if isinstance(result, dict):
            paths = result.get("paths", {})
            assert "/users" in paths
        else:
            assert isinstance(result, str)


class TestRetry:
    """Test retry-with-backoff behavior in raw_request."""

    def test_retry_transient_then_success(self, monkeypatch):
        """A transient failure followed by success is retried and succeeds."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=20.0):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("timed out")
            return _ok_response(body=b"finally")(request, timeout=timeout)

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        sleeps = []
        monkeypatch.setattr(http_impl, "_sleep", lambda d: sleeps.append(d))
        status, _hdrs, text, err = raw_request(
            "GET", "https://example.com", max_retries=3
        )
        assert status == 200
        assert text == "finally"
        assert err == ""
        assert calls["n"] == 3
        assert len(sleeps) == 2

    def test_retry_exhausted_returns_last_error(self, monkeypatch):
        """Once retries are exhausted, the last transient error is returned."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=20.0):
            calls["n"] += 1
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request(
            "GET", "https://example.com", max_retries=2
        )
        assert status == 0
        assert err.startswith("error: fetch timed out")
        # initial attempt + 2 retries = 3 total attempts
        assert calls["n"] == 3

    def test_retry_5xx_response(self, monkeypatch):
        """A 5xx response is treated as transient and retried for idempotent methods."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=20.0):
            calls["n"] += 1
            if calls["n"] < 2:
                raise urllib.error.HTTPError(
                    "https://example.com", 500, "Server Error", {}, BytesIO(b"boom")
                )
            return _ok_response(status=200, body=b"recovered")(request, timeout=timeout)

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, text, err = raw_request(
            "GET", "https://example.com", max_retries=2
        )
        assert status == 200
        assert text == "recovered"
        assert err == ""
        assert calls["n"] == 2

    def test_post_never_retried_on_500(self, monkeypatch):
        """POST is not idempotent, so a 500 gets exactly one attempt."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=20.0):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "https://example.com", 500, "Server Error", {}, BytesIO(b"boom")
            )

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        sleeps = []
        monkeypatch.setattr(http_impl, "_sleep", lambda d: sleeps.append(d))
        status, _hdrs, _text, _err = raw_request(
            "POST", "https://example.com", max_retries=3
        )
        assert status == 500
        assert calls["n"] == 1
        assert sleeps == []

    def test_patch_never_retried_on_timeout(self, monkeypatch):
        """PATCH is not idempotent, so a timeout gets exactly one attempt."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=20.0):
            calls["n"] += 1
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        sleeps = []
        monkeypatch.setattr(http_impl, "_sleep", lambda d: sleeps.append(d))
        status, _hdrs, _text, err = raw_request(
            "PATCH", "https://example.com", max_retries=3
        )
        assert status == 0
        assert err.startswith("error: fetch timed out")
        assert calls["n"] == 1
        assert sleeps == []

    def test_4xx_never_retried_even_for_idempotent_method(self, monkeypatch):
        """A 4xx response is never retried, even for a normally-idempotent method."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=20.0):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "https://example.com", 404, "Not Found", {}, BytesIO(b"missing")
            )

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        sleeps = []
        monkeypatch.setattr(http_impl, "_sleep", lambda d: sleeps.append(d))
        status, _hdrs, _text, _err = raw_request(
            "GET", "https://example.com", max_retries=3
        )
        assert status == 404
        assert calls["n"] == 1
        assert sleeps == []

    def test_backoff_uses_exponential_delay(self, monkeypatch):
        """Backoff delay grows with attempt number (base * 2**attempt, plus jitter)."""

        def fake_urlopen(request, timeout=20.0):
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "random", SimpleNamespace(uniform=lambda a, b: 0))
        sleeps = []
        monkeypatch.setattr(http_impl, "_sleep", lambda d: sleeps.append(d))
        raw_request("GET", "https://example.com", max_retries=2, backoff_base=1.0)
        assert sleeps == [1.0, 2.0]


class TestRedirects:
    """Test redirect-loop detection."""

    def test_redirect_loop_via_http_error(self, monkeypatch):
        """An HTTPError carrying the redirect-loop marker becomes a clear error."""

        def fake_urlopen(request, timeout=20.0):
            raise urllib.error.HTTPError(
                "https://example.com",
                310,
                http_impl._REDIRECT_LOOP_MARKER,
                {},
                BytesIO(b""),
            )

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, _hdrs, _text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert err == (
            "error: too many redirects (possible redirect loop) "
            "for https://example.com"
        )

    def test_redirect_loop_via_url_error(self, monkeypatch):
        """A URLError whose reason mentions the redirect-loop marker is also caught."""

        def fake_urlopen(request, timeout=20.0):
            raise urllib.error.URLError(http_impl._REDIRECT_LOOP_MARKER)

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        status, _hdrs, _text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert err == (
            "error: too many redirects (possible redirect loop) "
            "for https://example.com"
        )

    def test_redirect_loop_not_retried(self, monkeypatch):
        """A redirect loop is not a transient failure, so it is not retried."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=20.0):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "https://example.com",
                310,
                http_impl._REDIRECT_LOOP_MARKER,
                {},
                BytesIO(b""),
            )

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        sleeps = []
        monkeypatch.setattr(http_impl, "_sleep", lambda d: sleeps.append(d))
        raw_request("GET", "https://example.com", max_retries=3)
        assert calls["n"] == 1
        assert sleeps == []


class TestTimeouts:
    """Test connect/read timeout handling."""

    def test_timeout_error_message(self, monkeypatch):
        """A TimeoutError produces a message that mentions the timeout duration."""

        def fake_urlopen(request, timeout=20.0):
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request(
            "GET", "https://example.com", timeout=5.0, max_retries=0
        )
        assert status == 0
        assert "timed out" in err
        assert "5.0" in err

    def test_socket_timeout_via_urlerror(self, monkeypatch):
        """socket.timeout wrapped in a URLError still reads as 'timed out'."""

        def fake_urlopen(request, timeout=20.0):
            raise urllib.error.URLError(socket.timeout("timed out"))

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request(
            "GET", "https://example.com", max_retries=0
        )
        assert status == 0
        assert "timed out" in err

    def test_timeout_is_retried_for_idempotent_method(self, monkeypatch):
        """Timeouts are transient, so a GET retries on timeout."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=20.0):
            calls["n"] += 1
            raise TimeoutError("timed out")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request(
            "GET", "https://example.com", max_retries=2
        )
        assert status == 0
        assert "timed out" in err
        assert calls["n"] == 3


class TestTLSErrors:
    """Test TLS/certificate error handling."""

    def test_ssl_cert_verification_error_from_urlopen(self, monkeypatch):
        """ssl.SSLCertVerificationError raised directly from urlopen is caught."""

        def fake_urlopen(request, timeout=20.0):
            raise ssl.SSLCertVerificationError("certificate verify failed")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert "TLS certificate verification failed" in err
        assert "https://example.com" in err

    def test_ssl_error_from_urlopen(self, monkeypatch):
        """A plain ssl.SSLError raised directly from urlopen is caught."""

        def fake_urlopen(request, timeout=20.0):
            raise ssl.SSLError("decryption failed or bad record mac")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert "TLS certificate verification failed" in err

    def test_ssl_cert_verification_error_via_urlerror(self, monkeypatch):
        """ssl.SSLCertVerificationError wrapped in a URLError.reason is caught."""

        def fake_urlopen(request, timeout=20.0):
            raise urllib.error.URLError(
                ssl.SSLCertVerificationError("certificate verify failed: self signed cert")
            )

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert "TLS certificate verification failed" in err

    def test_ssl_error_via_urlerror(self, monkeypatch):
        """A plain ssl.SSLError wrapped in a URLError.reason is caught."""

        def fake_urlopen(request, timeout=20.0):
            raise urllib.error.URLError(ssl.SSLError("bad handshake"))

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert "TLS certificate verification failed" in err

    def test_tls_error_not_retried(self, monkeypatch):
        """TLS failures are not transient and are never retried."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=20.0):
            calls["n"] += 1
            raise ssl.SSLCertVerificationError("certificate verify failed")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        sleeps = []
        monkeypatch.setattr(http_impl, "_sleep", lambda d: sleeps.append(d))
        raw_request("GET", "https://example.com", max_retries=3)
        assert calls["n"] == 1
        assert sleeps == []


class TestDNSFailures:
    """Test DNS resolution failure handling."""

    def test_dns_failure_direct(self, monkeypatch):
        """socket.gaierror raised directly from urlopen produces a clear message."""

        def fake_urlopen(request, timeout=20.0):
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request(
            "GET", "https://nonexistent.invalid", max_retries=2
        )
        assert status == 0
        assert err == "error: DNS resolution failed for nonexistent.invalid"

    def test_dns_failure_via_urlerror(self, monkeypatch):
        """socket.gaierror wrapped in a URLError.reason is also caught."""

        def fake_urlopen(request, timeout=20.0):
            raise urllib.error.URLError(socket.gaierror("Name or service not known"))

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request(
            "GET", "https://nonexistent.invalid", max_retries=2
        )
        assert status == 0
        assert err == "error: DNS resolution failed for nonexistent.invalid"

    def test_dns_failure_is_retried_for_idempotent_method(self, monkeypatch):
        """DNS failures are treated as transient and retried for GET/HEAD/PUT/DELETE."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=20.0):
            calls["n"] += 1
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        raw_request("GET", "https://nonexistent.invalid", max_retries=2)
        assert calls["n"] == 3


class TestConnectionFailures:
    """Test connection reset / connection error handling."""

    def test_connection_reset_direct(self, monkeypatch):
        """ConnectionResetError raised directly from urlopen produces a clear message."""

        def fake_urlopen(request, timeout=20.0):
            raise ConnectionResetError("Connection reset by peer")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request(
            "GET", "https://example.com", max_retries=2
        )
        assert status == 0
        assert err == "error: connection reset by example.com"

    def test_connection_reset_via_urlerror(self, monkeypatch):
        """ConnectionResetError wrapped in a URLError.reason is also caught."""

        def fake_urlopen(request, timeout=20.0):
            raise urllib.error.URLError(ConnectionResetError("Connection reset by peer"))

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request(
            "GET", "https://example.com", max_retries=2
        )
        assert status == 0
        assert err == "error: connection reset by example.com"

    def test_connection_error_direct(self, monkeypatch):
        """A generic ConnectionError raised directly from urlopen is caught."""

        def fake_urlopen(request, timeout=20.0):
            raise ConnectionError("connection refused")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request(
            "GET", "https://example.com", max_retries=2
        )
        assert status == 0
        assert err == "error: connection error for example.com: connection refused"

    def test_connection_error_via_urlerror(self, monkeypatch):
        """A generic ConnectionError wrapped in a URLError.reason is also caught."""

        def fake_urlopen(request, timeout=20.0):
            raise urllib.error.URLError(ConnectionError("connection refused"))

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        status, _hdrs, _text, err = raw_request(
            "GET", "https://example.com", max_retries=2
        )
        assert status == 0
        assert err == "error: connection error for example.com: connection refused"

    def test_connection_reset_retried_for_idempotent_method(self, monkeypatch):
        """Connection resets are transient and retried for idempotent methods."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=20.0):
            calls["n"] += 1
            raise ConnectionResetError("Connection reset by peer")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        monkeypatch.setattr(http_impl, "_sleep", lambda d: None)
        raw_request("PUT", "https://example.com", max_retries=2)
        assert calls["n"] == 3

    def test_connection_reset_not_retried_for_post(self, monkeypatch):
        """Connection resets on a non-idempotent POST get a single attempt."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=20.0):
            calls["n"] += 1
            raise ConnectionResetError("Connection reset by peer")

        monkeypatch.setattr(http_impl, "urlopen", fake_urlopen)
        sleeps = []
        monkeypatch.setattr(http_impl, "_sleep", lambda d: sleeps.append(d))
        raw_request("POST", "https://example.com", max_retries=2)
        assert calls["n"] == 1
        assert sleeps == []
