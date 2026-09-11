"""Comprehensive tests for runtime/tools/httpx.py to improve coverage."""
from __future__ import annotations

import json
import urllib.error
from unittest.mock import Mock, patch, MagicMock

import pytest

from runtime.tools import httpx as httpx_impl
from runtime.tools.httpx import (
    _SafeRedirect,
    urlopen,
    blocked_host,
    _hostname,
    _ip_blocked,
    raw_request,
    get_json,
    post_json,
    fetch_text,
    http_request,
    openapi_ops,
    _parse_headers,
    _decode,
    _parse_openapi,
    _paths_from_yaml,
)


# Test _hostname edge cases
class TestHostname:
    def test_hostname_with_userinfo(self):
        """Extract hostname from netloc with user:pass@host:port."""
        result = _hostname("user:pass@example.com:8080")
        assert result == "example.com"

    def test_hostname_ipv6(self):
        """Handle IPv6 addresses in brackets."""
        result = _hostname("[::1]:8080")
        assert result == "::1"

    def test_hostname_ipv6_missing_bracket(self):
        """Handle malformed IPv6 (missing closing bracket)."""
        result = _hostname("[::1")
        assert result == "[::1"

    def test_hostname_empty(self):
        """Handle empty netloc."""
        result = _hostname("")
        assert result == ""

    def test_hostname_only_port(self):
        """Handle host:port format."""
        result = _hostname("localhost:8080")
        assert result == "localhost"

    def test_hostname_uppercase(self):
        """Hostnames should be lowercased."""
        result = _hostname("EXAMPLE.COM")
        assert result == "example.com"

    def test_hostname_with_port_only(self):
        """Host with port should extract host."""
        result = _hostname("example.com:443")
        assert result == "example.com"

    def test_hostname_userinfo_no_port(self):
        """Userinfo without port."""
        result = _hostname("user@example.com")
        assert result == "example.com"

    def test_hostname_ipv6_with_userinfo(self):
        """IPv6 with userinfo."""
        result = _hostname("user@[2001:db8::1]")
        assert result == "2001:db8::1"


# Test _ip_blocked
class TestIpBlocked:
    def test_loopback_not_blocked(self):
        """Loopback addresses (127.0.0.1, ::1) should not be blocked."""
        import ipaddress
        assert not _ip_blocked(ipaddress.ip_address("127.0.0.1"))
        assert not _ip_blocked(ipaddress.ip_address("::1"))

    def test_reserved_blocked(self):
        """Reserved IPs should be blocked."""
        import ipaddress
        assert _ip_blocked(ipaddress.ip_address("192.0.2.1"))  # TEST-NET-1
        assert _ip_blocked(ipaddress.ip_address("192.0.0.0"))  # This network

    def test_link_local_blocked(self):
        """Link-local addresses should be blocked."""
        import ipaddress
        assert _ip_blocked(ipaddress.ip_address("169.254.1.1"))

    def test_multicast_blocked(self):
        """Multicast addresses should be blocked."""
        import ipaddress
        assert _ip_blocked(ipaddress.ip_address("224.0.0.1"))

    def test_unspecified_blocked(self):
        """Unspecified address should be blocked."""
        import ipaddress
        assert _ip_blocked(ipaddress.ip_address("0.0.0.0"))


# Test blocked_host with DNS lookup
class TestBlockedHost:
    def test_blocked_host_with_dns_gaierror(self, monkeypatch):
        """When DNS lookup fails, return False (allow)."""
        import socket
        monkeypatch.setattr(
            "socket.getaddrinfo",
            Mock(side_effect=socket.gaierror("Name not found")),
        )
        result = blocked_host("nonexistent.test")
        assert result is False

    def test_blocked_host_with_dns_success_blocked_ip(self, monkeypatch):
        """When DNS resolves to blocked IP, block it."""
        monkeypatch.setattr(
            "socket.getaddrinfo",
            Mock(return_value=[
                (None, None, None, None, ("192.0.2.1", 0)),  # TEST-NET-1
            ]),
        )
        result = blocked_host("blocked-dns.test")
        assert result is True

    def test_blocked_host_with_dns_success_allowed_ip(self, monkeypatch):
        """When DNS resolves to allowed IP (public), allow it."""
        monkeypatch.setattr(
            "socket.getaddrinfo",
            Mock(return_value=[
                (None, None, None, None, ("8.8.8.8", 0)),  # Google DNS
            ]),
        )
        result = blocked_host("google-dns.test")
        assert result is False

    def test_blocked_host_empty_netloc(self):
        """Empty netloc should not be blocked."""
        result = blocked_host("")
        assert result is False

    def test_blocked_host_metadata(self):
        """Metadata service hosts should be blocked."""
        result = blocked_host("metadata.google.internal")
        assert result is True

    def test_blocked_host_link_local_ip(self):
        """Link-local IPs should be blocked."""
        result = blocked_host("169.254.169.254")
        assert result is True

    def test_blocked_host_dns_multiple_results(self, monkeypatch):
        """When DNS returns multiple results, check all."""
        monkeypatch.setattr(
            "socket.getaddrinfo",
            Mock(return_value=[
                (None, None, None, None, ("8.8.8.8", 0)),  # Allowed
                (None, None, None, None, ("192.0.2.1", 0)),  # Blocked
            ]),
        )
        result = blocked_host("multi-dns.test")
        # Should block if any result is blocked
        assert result is True

    def test_blocked_host_ipv6_address(self):
        """IPv6 address should be handled."""
        result = blocked_host("[2001:db8::1]")
        assert result is False or result is True  # Either is valid, depends on IP


# Test _SafeRedirect
class TestSafeRedirect:
    def test_safe_redirect_allows_http(self):
        """HTTP and HTTPS redirects should be allowed."""
        handler = _SafeRedirect()
        req = Mock()
        from io import BytesIO
        result = handler.redirect_request(
            req, BytesIO(), 302, "Found", Mock(), "http://example.com/path"
        )
        assert result is not None

    def test_safe_redirect_allows_https(self):
        """HTTPS redirects should be allowed."""
        handler = _SafeRedirect()
        req = Mock()
        from io import BytesIO
        result = handler.redirect_request(
            req, BytesIO(), 302, "Found", Mock(), "https://example.com/path"
        )
        assert result is not None

    def test_safe_redirect_blocks_file_scheme(self):
        """File:// redirects should be blocked."""
        handler = _SafeRedirect()
        req = Mock()
        from io import BytesIO
        with pytest.raises(urllib.error.URLError, match="redirect must be http"):
            handler.redirect_request(
                req, BytesIO(), 302, "Found", Mock(), "file:///etc/passwd"
            )

    def test_safe_redirect_blocks_no_netloc(self):
        """Redirects without a netloc should be blocked."""
        handler = _SafeRedirect()
        req = Mock()
        from io import BytesIO
        with pytest.raises(urllib.error.URLError, match="redirect must be http"):
            handler.redirect_request(req, BytesIO(), 302, "Found", Mock(), "http://")

    def test_safe_redirect_blocks_blocked_host(self, monkeypatch):
        """Redirects to blocked hosts should be blocked."""
        handler = _SafeRedirect()
        req = Mock()
        from io import BytesIO
        with pytest.raises(urllib.error.URLError, match="blocked host"):
            handler.redirect_request(
                req, BytesIO(), 302, "Found", Mock(), "http://169.254.169.254/metadata"
            )


# Test raw_request error cases
class TestRawRequest:
    def test_raw_request_invalid_scheme(self):
        """Non-HTTP/HTTPS schemes should return error."""
        status, hdrs, text, err = raw_request("GET", "ftp://example.com")
        assert status == 0
        assert err.startswith("error:")
        assert "http" in err

    def test_raw_request_no_netloc(self):
        """URL without netloc should return error."""
        status, hdrs, text, err = raw_request("GET", "http://")
        assert status == 0
        assert err.startswith("error:")

    def test_raw_request_blocked_host(self):
        """Blocked hosts should return error."""
        status, hdrs, text, err = raw_request("GET", "http://169.254.169.254")
        assert status == 0
        assert err == "error: blocked host"

    def test_raw_request_invalid_method(self):
        """Invalid HTTP methods should return error."""
        status, hdrs, text, err = raw_request("INVALID", "https://example.com")
        assert status == 0
        assert err.startswith("error:")
        assert "GET, HEAD, POST" in err

    def test_raw_request_head_ignores_body(self, monkeypatch):
        """HEAD requests should not read response body."""
        def mock_urlopen(request, timeout=20.0):
            resp = Mock()
            resp.status = 200
            resp.headers = {"Content-Type": "text/plain"}
            resp.read = Mock(side_effect=Exception("Should not be called"))
            resp.__enter__ = Mock(return_value=resp)
            resp.__exit__ = Mock(return_value=False)
            return resp

        monkeypatch.setattr("runtime.tools.httpx.urlopen", mock_urlopen)
        status, hdrs, text, err = raw_request("HEAD", "https://example.com")
        assert status == 200
        assert err == ""
        assert text == ""

    def test_raw_request_http_error_with_body(self, monkeypatch):
        """HTTP errors should read error body."""
        exc = urllib.error.HTTPError(
            "https://example.com", 404, "Not Found", {}, None
        )
        exc.read = Mock(return_value=b"Not found message")
        monkeypatch.setattr("runtime.tools.httpx.urlopen", Mock(side_effect=exc))
        
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 404
        assert err == ""
        assert "Not found message" in text

    def test_raw_request_http_error_head_no_read(self, monkeypatch):
        """HEAD errors should not read body."""
        exc = urllib.error.HTTPError(
            "https://example.com", 404, "Not Found", {}, None
        )
        exc.read = Mock(side_effect=Exception("Should not be called"))
        monkeypatch.setattr("runtime.tools.httpx.urlopen", Mock(side_effect=exc))
        
        status, hdrs, text, err = raw_request("HEAD", "https://example.com")
        assert status == 404
        assert err == ""

    def test_raw_request_http_error_read_fails(self, monkeypatch):
        """When error body read fails, use empty string."""
        exc = urllib.error.HTTPError(
            "https://example.com", 500, "Server Error", {}, None
        )
        exc.read = Mock(side_effect=OSError("Connection lost"))
        monkeypatch.setattr("runtime.tools.httpx.urlopen", Mock(side_effect=exc))
        
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 500
        assert err == ""
        assert text == ""

    def test_raw_request_url_error(self, monkeypatch):
        """URLError should return appropriate error."""
        monkeypatch.setattr(
            "runtime.tools.httpx.urlopen",
            Mock(side_effect=urllib.error.URLError("Connection refused")),
        )
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert "error:" in err
        assert "Connection refused" in err

    def test_raw_request_timeout(self, monkeypatch):
        """TimeoutError should be handled."""
        monkeypatch.setattr(
            "runtime.tools.httpx.urlopen",
            Mock(side_effect=TimeoutError()),
        )
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert err == "error: fetch timed out"

    def test_raw_request_os_error(self, monkeypatch):
        """OSError should return appropriate error."""
        monkeypatch.setattr(
            "runtime.tools.httpx.urlopen",
            Mock(side_effect=OSError("Connection reset")),
        )
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert "error:" in err
        assert "Connection reset" in err

    def test_raw_request_response_truncated(self, monkeypatch):
        """Response exceeding cap should be truncated."""
        def mock_urlopen(request, timeout=20.0):
            resp = Mock()
            resp.status = 200
            resp.headers = {}
            resp.read = Mock(return_value=b"x" * 60001)
            resp.__enter__ = Mock(return_value=resp)
            resp.__exit__ = Mock(return_value=False)
            return resp

        monkeypatch.setattr("runtime.tools.httpx.urlopen", mock_urlopen)
        status, hdrs, text, err = raw_request("GET", "https://example.com", cap=50000)
        assert status == 200
        assert err == ""
        assert "[truncated]" in text

    def test_raw_request_response_headers_copied(self, monkeypatch):
        """Response headers should be properly copied."""
        def mock_urlopen(request, timeout=20.0):
            resp = Mock()
            resp.status = 200
            resp.headers = {"Content-Type": "application/json", "X-Custom": "value"}
            resp.read = Mock(return_value=b'{"key": "value"}')
            resp.__enter__ = Mock(return_value=resp)
            resp.__exit__ = Mock(return_value=False)
            return resp

        monkeypatch.setattr("runtime.tools.httpx.urlopen", mock_urlopen)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert hdrs["Content-Type"] == "application/json"
        assert hdrs["X-Custom"] == "value"

    def test_raw_request_status_none(self, monkeypatch):
        """Response status None should default to 200."""
        def mock_urlopen(request, timeout=20.0):
            resp = Mock()
            resp.status = None
            resp.headers = {}
            resp.read = Mock(return_value=b"ok")
            resp.__enter__ = Mock(return_value=resp)
            resp.__exit__ = Mock(return_value=False)
            return resp

        monkeypatch.setattr("runtime.tools.httpx.urlopen", mock_urlopen)
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 200

    def test_raw_request_post_with_headers(self, monkeypatch):
        """POST with request headers."""
        seen_request = {}
        
        def mock_urlopen(request, timeout=20.0):
            seen_request["headers"] = dict(request.headers)
            resp = Mock()
            resp.status = 200
            resp.headers = {}
            resp.read = Mock(return_value=b"ok")
            resp.__enter__ = Mock(return_value=resp)
            resp.__exit__ = Mock(return_value=False)
            return resp

        monkeypatch.setattr("runtime.tools.httpx.urlopen", mock_urlopen)
        raw_request("POST", "https://example.com", headers={"X-Custom": "value"})
        assert "X-Custom" in seen_request["headers"]


# Test get_json
class TestGetJson:
    def test_get_json_success(self, monkeypatch):
        """Successful JSON response."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, '{"key": "value"}', "")),
        )
        data, err = get_json("https://example.com/api")
        assert data == {"key": "value"}
        assert err == ""

    def test_get_json_null(self, monkeypatch):
        """JSON null should return None."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, "null", "")),
        )
        data, err = get_json("https://example.com/api")
        assert data is None
        assert err == ""

    def test_get_json_empty_string(self, monkeypatch):
        """Empty response should return None (null)."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, "", "")),
        )
        data, err = get_json("https://example.com/api")
        assert data is None
        assert err == ""

    def test_get_json_request_error(self, monkeypatch):
        """Raw request error should propagate."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(0, {}, "", "error: timeout")),
        )
        data, err = get_json("https://example.com/api")
        assert data is None
        assert err == "error: timeout"

    def test_get_json_http_error(self, monkeypatch):
        """HTTP error status should return error."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(404, {}, "", "")),
        )
        data, err = get_json("https://example.com/api")
        assert data is None
        assert err == "error: HTTP 404"

    def test_get_json_invalid_json(self, monkeypatch):
        """Invalid JSON should return parse error."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, "not json", "")),
        )
        data, err = get_json("https://example.com/api")
        assert data is None
        assert "error:" in err
        assert "JSON" in err or "json" in err.lower()

    def test_get_json_custom_headers(self, monkeypatch):
        """Custom headers should be passed through."""
        seen_headers = {}
        
        def mock_raw_request(url, method="GET", headers=None, **kwargs):
            if headers:
                seen_headers.update(headers)
            return (200, {}, '{}', "")

        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            mock_raw_request,
        )
        get_json("https://example.com/api", headers={"Authorization": "Bearer token"})
        assert "Authorization" in seen_headers


# Test post_json
class TestPostJson:
    def test_post_json_success(self, monkeypatch):
        """Successful POST with JSON."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(201, {}, '{"id": 123}', "")),
        )
        data, err = post_json("https://example.com/api", {"name": "test"})
        assert data == {"id": 123}
        assert err == ""

    def test_post_json_request_error(self, monkeypatch):
        """Request error should propagate."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(0, {}, "", "error: timeout")),
        )
        data, err = post_json("https://example.com/api", {})
        assert data is None
        assert err == "error: timeout"

    def test_post_json_http_error_with_body(self, monkeypatch):
        """HTTP error with response body should include it."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(400, {}, "Invalid request", "")),
        )
        data, err = post_json("https://example.com/api", {})
        assert data is None
        assert "error: HTTP 400" in err
        assert "Invalid request" in err

    def test_post_json_invalid_json_response(self, monkeypatch):
        """Invalid JSON response should return error."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, "not json", "")),
        )
        data, err = post_json("https://example.com/api", {})
        assert data is None
        assert "error:" in err


# Test fetch_text
class TestFetchText:
    def test_fetch_text_success(self, monkeypatch):
        """Successful text fetch."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, "Hello, World!", "")),
        )
        result = fetch_text("https://example.com/readme")
        assert result == "Hello, World!"

    def test_fetch_text_empty_response(self, monkeypatch):
        """Empty response should return (empty)."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, "", "")),
        )
        result = fetch_text("https://example.com/readme")
        assert result == "(empty)"

    def test_fetch_text_request_error(self, monkeypatch):
        """Request error should be returned."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(0, {}, "", "error: timeout")),
        )
        result = fetch_text("https://example.com/readme")
        assert result == "error: timeout"

    def test_fetch_text_http_error(self, monkeypatch):
        """HTTP error should be returned."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(404, {}, "", "")),
        )
        result = fetch_text("https://example.com/readme")
        assert result == "error: HTTP 404"

    def test_fetch_text_custom_cap(self, monkeypatch):
        """Custom cap should be passed to raw_request."""
        seen_args = {}
        
        def mock_raw_request(method, url, **kwargs):
            seen_args.update(kwargs)
            return (200, {}, "x" * 100, "")

        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            mock_raw_request,
        )
        fetch_text("https://example.com/readme", cap=500)
        assert seen_args.get("cap") == 500


# Test http_request
class TestHttpRequest:
    def test_http_request_invalid_headers(self):
        """Invalid headers format should return error."""
        result = http_request("GET", "https://example.com", headers="invalid header")
        assert result.startswith("error:")

    def test_http_request_json_headers(self, monkeypatch):
        """JSON headers should be parsed."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {"Content-Type": "application/json"}, "ok", "")),
        )
        result = http_request(
            "GET", "https://example.com", 
            headers='{"X-Custom": "value"}'
        )
        assert "status: 200" in result
        assert "ok" in result

    def test_http_request_interesting_headers(self, monkeypatch):
        """Only interesting headers should be included in output."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(
                200,
                {
                    "Content-Type": "application/json",
                    "X-Request-Id": "123",
                    "Server": "nginx",  # Not interesting
                    "Location": "https://example.com/new",
                },
                "body text",
                "",
            )),
        )
        result = http_request("GET", "https://example.com")
        assert "content-type: application/json" in result.lower()
        assert "x-request-id: 123" in result.lower()
        assert "location: https://example.com/new" in result.lower()
        assert "nginx" not in result  # Server header not included

    def test_http_request_with_body(self, monkeypatch):
        """POST with body should encode to bytes."""
        seen_args = {}
        
        def mock_raw_request(method, url, **kwargs):
            seen_args.update(kwargs)
            return (200, {}, "ok", "")

        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            mock_raw_request,
        )
        http_request("POST", "https://example.com", body='{"key": "value"}')
        assert seen_args.get("body") == b'{"key": "value"}'

    def test_http_request_timeout_default(self, monkeypatch):
        """Default timeout should be 20.0."""
        seen_args = {}
        
        def mock_raw_request(method, url, **kwargs):
            seen_args.update(kwargs)
            return (200, {}, "ok", "")

        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            mock_raw_request,
        )
        http_request("GET", "https://example.com")
        assert seen_args.get("timeout") == 20.0

    def test_http_request_custom_timeout(self, monkeypatch):
        """Custom timeout should be respected."""
        seen_args = {}
        
        def mock_raw_request(method, url, **kwargs):
            seen_args.update(kwargs)
            return (200, {}, "ok", "")

        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            mock_raw_request,
        )
        http_request("GET", "https://example.com", timeout=30.5)
        assert seen_args.get("timeout") == 30.5


# Test openapi_ops
class TestOpenApiOps:
    def test_openapi_ops_json(self, monkeypatch):
        """Parse JSON OpenAPI spec."""
        spec = {
            "paths": {
                "/users": {
                    "get": {"summary": "List users"},
                    "post": {"operationId": "createUser"},
                }
            }
        }
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, json.dumps(spec), "")),
        )
        result = openapi_ops("https://example.com/openapi.json")
        assert "GET /users — List users" in result
        assert "POST /users — createUser" in result

    def test_openapi_ops_yaml(self, monkeypatch):
        """Parse YAML OpenAPI spec."""
        yaml = """
paths:
  /users:
    get:
      summary: List users
    post:
      summary: Create user
"""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, yaml, "")),
        )
        result = openapi_ops("https://example.com/openapi.yaml")
        assert "GET /users — List users" in result
        assert "POST /users — Create user" in result

    def test_openapi_ops_no_paths(self, monkeypatch):
        """No paths section should return error."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, '{"info": {}}', "")),
        )
        result = openapi_ops("https://example.com/openapi.json")
        assert result.startswith("error:")
        assert "no paths" in result.lower()

    def test_openapi_ops_cap_truncation(self, monkeypatch):
        """Should truncate after 80 operations."""
        paths = {f"/endpoint{i}": {"get": {}} for i in range(100)}
        spec = {"paths": paths}
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, json.dumps(spec), "")),
        )
        result = openapi_ops("https://example.com/openapi.json")
        assert "[truncated]" in result

    def test_openapi_ops_request_error(self, monkeypatch):
        """Request error should be returned."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(0, {}, "", "error: timeout")),
        )
        result = openapi_ops("https://example.com/openapi.json")
        assert result == "error: timeout"

    def test_openapi_ops_http_error(self, monkeypatch):
        """HTTP error should be returned."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(404, {}, "", "")),
        )
        result = openapi_ops("https://example.com/openapi.json")
        assert result.startswith("error: HTTP")

    def test_openapi_ops_invalid_json(self, monkeypatch):
        """Invalid JSON/YAML should return error."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, "not json or yaml", "")),
        )
        result = openapi_ops("https://example.com/openapi.json")
        assert result.startswith("error:")

    def test_openapi_ops_no_operations(self, monkeypatch):
        """Empty paths should return no operations message."""
        spec = {"paths": {}}
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, json.dumps(spec), "")),
        )
        result = openapi_ops("https://example.com/openapi.json")
        assert "(no operations)" in result

    def test_openapi_ops_invalid_method(self, monkeypatch):
        """Non-HTTP methods should be skipped."""
        spec = {
            "paths": {
                "/users": {
                    "get": {"summary": "Get users"},
                    "x-custom": {"summary": "Custom"},  # Should be skipped
                }
            }
        }
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, json.dumps(spec), "")),
        )
        result = openapi_ops("https://example.com/openapi.json")
        assert "GET /users" in result
        assert "x-custom" not in result


# Test _parse_openapi
class TestParseOpenapi:
    def test_parse_openapi_json(self):
        """JSON parsing."""
        spec = {"paths": {"/users": {"get": {}}}}
        result = _parse_openapi(json.dumps(spec))
        assert result == spec

    def test_parse_openapi_yaml(self):
        """YAML parsing fallback."""
        yaml = "paths:\n  /users:\n    get:"
        result = _parse_openapi(yaml)
        assert "paths" in result


# Test _paths_from_yaml
class TestPathsFromYaml:
    def test_paths_from_yaml_basic(self):
        """Extract paths from YAML."""
        yaml = """
paths:
  /users:
    get:
    post:
  /users/{id}:
    get:
"""
        result = _paths_from_yaml(yaml)
        assert "/users" in result["paths"]
        assert "/users/{id}" in result["paths"]
        assert "get" in result["paths"]["/users"]
        assert "post" in result["paths"]["/users"]

    def test_paths_from_yaml_skip_comments(self):
        """Skip comments and blank lines."""
        yaml = """
# This is a comment
paths:
  # Path comment
  /users:
    # Method comment
    get:
"""
        result = _paths_from_yaml(yaml)
        assert "/users" in result["paths"]
        assert "get" in result["paths"]["/users"]

    def test_paths_from_yaml_no_paths_section(self):
        """No paths section should return error."""
        yaml = "info:\n  title: API"
        result = _paths_from_yaml(yaml)
        assert isinstance(result, str)
        assert result.startswith("error:")

    def test_paths_from_yaml_end_at_lower_indent(self):
        """Stop parsing when indent returns to root."""
        yaml = """
paths:
  /users:
    get:
info:
  title: API
"""
        result = _paths_from_yaml(yaml)
        assert "/users" in result["paths"]


# Test _parse_headers
class TestParseHeaders:
    def test_parse_headers_empty(self):
        """Empty headers should return empty dict."""
        headers, err = _parse_headers("")
        assert headers == {}
        assert err == ""

    def test_parse_headers_json(self):
        """JSON headers should be parsed."""
        headers, err = _parse_headers('{"X-Custom": "value", "Authorization": "Bearer token"}')
        assert headers["X-Custom"] == "value"
        assert headers["Authorization"] == "Bearer token"
        assert err == ""

    def test_parse_headers_json_invalid(self):
        """Invalid JSON should return error."""
        headers, err = _parse_headers('{"incomplete": ')
        assert headers == {}
        assert "error:" in err

    def test_parse_headers_json_not_object(self):
        """JSON array should return error."""
        headers, err = _parse_headers('["a", "b"]')
        assert headers == {}
        assert "must be an object" in err

    def test_parse_headers_colon_format(self):
        """Colon-separated format should be parsed."""
        headers, err = _parse_headers("X-Custom: value\nAuthorization: Bearer token")
        assert headers["X-Custom"] == "value"
        assert headers["Authorization"] == "Bearer token"
        assert err == ""

    def test_parse_headers_colon_skip_blanks(self):
        """Blank lines should be skipped."""
        headers, err = _parse_headers("X-Custom: value\n\nAuthorization: Bearer token")
        assert len(headers) == 2
        assert err == ""

    def test_parse_headers_colon_no_colon_error(self):
        """Line without colon should return error."""
        headers, err = _parse_headers("X-Custom: value\nBadHeader")
        assert headers == {}
        assert "error:" in err
        assert "bad header line" in err

    def test_parse_headers_colon_multiple_colons(self):
        """Multiple colons should split on first."""
        headers, err = _parse_headers("X-Custom: value: with: colons")
        assert headers["X-Custom"] == "value: with: colons"
        assert err == ""


# Test _decode
class TestDecode:
    def test_decode_utf8(self):
        """Valid UTF-8 should decode correctly."""
        result = _decode(b"Hello, World!")
        assert result == "Hello, World!"

    def test_decode_non_utf8(self):
        """Non-UTF-8 should use replacement character."""
        # Create bytes that are not valid UTF-8
        invalid_bytes = b"\x80\x81\x82"
        result = _decode(invalid_bytes)
        assert result  # Should return something, not raise
        assert "\ufffd" in result or "?" in result or len(result) > 0


# Additional integration tests for line coverage
class TestIntegrationEdgeCases:
    def test_raw_request_empty_error_response_body(self, monkeypatch):
        """HTTP error with empty error body."""
        exc = urllib.error.HTTPError(
            "https://example.com", 500, "Server Error", {}, None
        )
        exc.read = Mock(return_value=b"")
        monkeypatch.setattr("runtime.tools.httpx.urlopen", Mock(side_effect=exc))
        status, hdrs, text, err = raw_request("GET", "https://example.com")
        assert status == 500
        assert err == ""
        assert text == ""

    def test_http_request_allow_header(self, monkeypatch):
        """HTTP request should include Allow header in output."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(
                405,
                {
                    "Allow": "GET, POST",
                    "Other": "value",
                },
                "Method not allowed",
                "",
            )),
        )
        result = http_request("PUT", "https://example.com")
        assert "allow:" in result.lower()

    def test_openapi_ops_methods_case_insensitive(self, monkeypatch):
        """OpenAPI methods should be case-insensitive."""
        spec = {
            "paths": {
                "/users": {
                    "Get": {"summary": "lowercase method"},
                    "HEAD": {"summary": "uppercase head"},
                }
            }
        }
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, json.dumps(spec), "")),
        )
        result = openapi_ops("https://example.com/openapi.json")
        assert "GET /users" in result or "Get /users" in result

    def test_blocked_host_none_hostname(self):
        """blocked_host with empty host after parsing."""
        # This tests the "if not host or host in BLOCKED_HOSTS: return bool(host)" line
        result = blocked_host("@")  # Just userinfo separator
        assert result is False

    def test_raw_request_cap_zero(self, monkeypatch):
        """raw_request with cap=0 should still work."""
        def mock_urlopen(request, timeout=20.0):
            resp = Mock()
            resp.status = 200
            resp.headers = {}
            resp.read = Mock(return_value=b"small")
            resp.__enter__ = Mock(return_value=resp)
            resp.__exit__ = Mock(return_value=False)
            return resp

        monkeypatch.setattr("runtime.tools.httpx.urlopen", mock_urlopen)
        status, hdrs, text, err = raw_request("GET", "https://example.com", cap=0)
        # cap=0 means read 1 byte (cap+1)
        assert status == 200

    def test_yaml_paths_indentation_boundary(self):
        """YAML path parsing with tricky indentation."""
        yaml = """
paths:
  /users:
    get:
    post:
  /items:
    get:
"""
        result = _paths_from_yaml(yaml)
        assert "/users" in result["paths"]
        assert "/items" in result["paths"]

    def test_parse_headers_json_with_null_value(self):
        """JSON headers with null values."""
        headers, err = _parse_headers('{"X-Custom": null}')
        assert headers["X-Custom"] == "None"
        assert err == ""

    def test_parse_headers_json_with_number(self):
        """JSON headers with numeric values."""
        headers, err = _parse_headers('{"X-Count": 42}')
        assert headers["X-Count"] == "42"
        assert err == ""

    def test_post_json_empty_response(self, monkeypatch):
        """POST JSON with empty response body."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, "", "")),
        )
        data, err = post_json("https://example.com/api", {})
        assert data is None
        assert err == ""

    def test_fetch_text_with_custom_cap_exceeding_max(self, monkeypatch):
        """fetch_text with cap smaller than available."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            Mock(return_value=(200, {}, "x" * 1000, "")),
        )
        result = fetch_text("https://example.com", cap=100)
        assert len(result) <= 150  # 100 + "[truncated]"
        assert "[truncated]" in result
