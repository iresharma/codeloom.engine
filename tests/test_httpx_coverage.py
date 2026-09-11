"""Tests for runtime/tools/httpx.py to improve coverage.

Focuses on missing lines from baseline coverage report:
- Lines 25-30, 34-35, 48-49, 57, 59-60, 69, 87-120, 124-135, 139-154, 158-163
- 176, 202, 204, 207, 210, 214, 217, 226-227, 234-235, 239-264, 272-287, 291-294
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse

from runtime.tools import httpx
from runtime.tools.httpx import (
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
    _SafeRedirect,
    _parse_openapi,
    _paths_from_yaml,
    urlopen,
)


class TestSafeRedirect:
    """Tests for _SafeRedirect redirect_request."""

    def test_safe_redirect_accepts_https(self):
        """Test that HTTPS redirects are allowed."""
        handler = _SafeRedirect()
        req = Mock()
        fp = Mock()
        headers = {}
        # Should not raise
        result = handler.redirect_request(req, fp, 301, "Moved", headers, "https://example.com/new")
        assert result is not None

    def test_safe_redirect_accepts_http(self):
        """Test that HTTP redirects are allowed."""
        handler = _SafeRedirect()
        req = Mock()
        fp = Mock()
        headers = {}
        result = handler.redirect_request(req, fp, 301, "Moved", headers, "http://example.com/new")
        assert result is not None

    def test_safe_redirect_rejects_file_scheme(self):
        """Test that file:// redirects are rejected."""
        handler = _SafeRedirect()
        req = Mock()
        fp = Mock()
        headers = {}
        with pytest.raises(urllib.error.URLError, match="redirect must be http or https"):
            handler.redirect_request(req, fp, 301, "Moved", headers, "file:///etc/passwd")

    def test_safe_redirect_rejects_data_scheme(self):
        """Test that data: redirects are rejected."""
        handler = _SafeRedirect()
        req = Mock()
        fp = Mock()
        headers = {}
        with pytest.raises(urllib.error.URLError, match="redirect must be http or https"):
            handler.redirect_request(req, fp, 301, "Moved", headers, "data:text/html,<h1>x</h1>")

    def test_safe_redirect_blocks_metadata_hosts(self):
        """Test that metadata hosts are blocked."""
        handler = _SafeRedirect()
        req = Mock()
        fp = Mock()
        headers = {}
        with pytest.raises(urllib.error.URLError, match="blocked host"):
            handler.redirect_request(req, fp, 301, "Moved", headers, "https://169.254.169.254/meta")

    def test_safe_redirect_blocks_google_metadata(self):
        """Test that Google metadata hosts are blocked."""
        handler = _SafeRedirect()
        req = Mock()
        fp = Mock()
        headers = {}
        with pytest.raises(urllib.error.URLError, match="blocked host"):
            handler.redirect_request(req, fp, 301, "Moved", headers, "https://metadata.google.internal/")


class TestHostname:
    """Tests for _hostname parsing."""

    def test_hostname_with_userinfo(self):
        """Test hostname extraction from netloc with user info."""
        # Line 56-57: host with @ sign
        result = _hostname("user:pass@example.com")
        assert result == "example.com"

    def test_hostname_ipv6(self):
        """Test hostname extraction from IPv6."""
        # Line 58-60: IPv6 bracket notation
        result = _hostname("[::1]")
        assert result == "::1"

    def test_hostname_ipv6_invalid(self):
        """Test invalid IPv6 bracket notation."""
        # Line 58-60: IPv6 with unmatched bracket
        result = _hostname("[::1")
        assert result == "[::1"

    def test_hostname_with_port(self):
        """Test hostname extraction removes port."""
        # Line 61-62: hostname with port
        result = _hostname("example.com:8080")
        assert result == "example.com"

    def test_hostname_empty(self):
        """Test empty hostname."""
        result = _hostname("")
        assert result == ""

    def test_hostname_uppercase(self):
        """Test hostname is lowercased."""
        result = _hostname("EXAMPLE.COM")
        assert result == "example.com"


class TestIPBlocked:
    """Tests for _ip_blocked."""

    def test_loopback_not_blocked(self):
        """Test that loopback addresses are not blocked."""
        import ipaddress
        # Line 67-68: loopback check
        ip = ipaddress.ip_address("127.0.0.1")
        assert not _ip_blocked(ip)
        ip6 = ipaddress.ip_address("::1")
        assert not _ip_blocked(ip6)

    def test_link_local_blocked(self):
        """Test that link-local addresses are blocked."""
        import ipaddress
        # Line 69: link-local check
        ip = ipaddress.ip_address("169.254.1.1")
        assert _ip_blocked(ip)

    def test_multicast_blocked(self):
        """Test that multicast addresses are blocked."""
        import ipaddress
        ip = ipaddress.ip_address("224.0.0.1")
        assert _ip_blocked(ip)

    def test_unspecified_blocked(self):
        """Test that unspecified addresses are blocked."""
        import ipaddress
        ip = ipaddress.ip_address("0.0.0.0")
        assert _ip_blocked(ip)

    def test_reserved_blocked(self):
        """Test that reserved addresses are blocked."""
        import ipaddress
        ip = ipaddress.ip_address("10.0.0.1")
        assert _ip_blocked(ip)

    def test_public_not_blocked(self):
        """Test that public addresses are not blocked."""
        import ipaddress
        ip = ipaddress.ip_address("8.8.8.8")
        assert not _ip_blocked(ip)


class TestBlockedHost:
    """Tests for blocked_host function."""

    def test_blocked_host_with_dns_lookup_failure(self, monkeypatch):
        """Test blocked_host with DNS failure (gaierror)."""
        # Line 48-49: socket.gaierror exception path
        def fake_getaddrinfo(*args, **kwargs):
            import socket
            raise socket.gaierror("Name resolution failed")
        
        monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
        result = blocked_host("nonexistent.invalid.local")
        assert result is False  # DNS failure returns False


class TestRawRequest:
    """Tests for raw_request function."""

    def test_raw_request_invalid_scheme(self):
        """Test raw_request rejects non-http schemes."""
        # Line 84: invalid scheme check
        status, headers, text, err = raw_request("GET", "ftp://example.com")
        assert status == 0
        assert err.startswith("error: url must be http or https")

    def test_raw_request_missing_netloc(self):
        """Test raw_request rejects missing netloc."""
        # Line 83-84: missing netloc
        status, headers, text, err = raw_request("GET", "http://")
        assert status == 0
        assert err.startswith("error: url must be http or https")

    def test_raw_request_blocked_host(self):
        """Test raw_request blocks metadata hosts."""
        # Line 85-86: blocked host check
        status, headers, text, err = raw_request("GET", "http://169.254.169.254/")
        assert status == 0
        assert err == "error: blocked host"

    def test_raw_request_invalid_method(self):
        """Test raw_request rejects invalid methods."""
        # Line 87-89: method validation
        status, headers, text, err = raw_request("INVALID", "https://example.com")
        assert status == 0
        assert "error: method must be one of" in err

    def test_raw_request_timeout(self, monkeypatch):
        """Test raw_request handles timeout."""
        # Line 112-113: TimeoutError handling
        def fake_urlopen(*args, **kwargs):
            raise TimeoutError("Request timed out")
        
        monkeypatch.setattr("runtime.tools.httpx.urlopen", fake_urlopen)
        status, headers, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert err == "error: fetch timed out"

    def test_raw_request_url_error(self, monkeypatch):
        """Test raw_request handles URLError."""
        # Line 110-111: URLError handling
        def fake_urlopen(*args, **kwargs):
            raise urllib.error.URLError("Connection refused")
        
        monkeypatch.setattr("runtime.tools.httpx.urlopen", fake_urlopen)
        status, headers, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert "error:" in err

    def test_raw_request_os_error(self, monkeypatch):
        """Test raw_request handles OSError."""
        # Line 114-115: OSError handling
        def fake_urlopen(*args, **kwargs):
            raise OSError("Connection failed")
        
        monkeypatch.setattr("runtime.tools.httpx.urlopen", fake_urlopen)
        status, headers, text, err = raw_request("GET", "https://example.com")
        assert status == 0
        assert "error:" in err

    def test_raw_request_http_error_with_body(self, monkeypatch):
        """Test raw_request handles HTTPError with response body."""
        # Line 99-109: HTTPError with body
        exc = urllib.error.HTTPError(
            "https://example.com", 404, "Not Found", {}, None
        )
        # Mock read to provide error body
        exc.read = Mock(return_value=b"Not found")
        
        def fake_urlopen(*args, **kwargs):
            raise exc
        
        monkeypatch.setattr("runtime.tools.httpx.urlopen", fake_urlopen)
        status, headers, text, err = raw_request("GET", "https://example.com")
        assert status == 404
        assert "Not found" in text
        assert err == ""

    def test_raw_request_http_error_head_no_body(self, monkeypatch):
        """Test raw_request HEAD doesn't try to read error body."""
        # Line 101: HEAD method doesn't read error body
        exc = urllib.error.HTTPError(
            "https://example.com", 404, "Not Found", {}, None
        )
        exc.read = Mock()
        
        def fake_urlopen(*args, **kwargs):
            raise exc
        
        monkeypatch.setattr("runtime.tools.httpx.urlopen", fake_urlopen)
        status, headers, text, err = raw_request("HEAD", "https://example.com")
        assert status == 404
        exc.read.assert_not_called()

    def test_raw_request_http_error_read_fails(self, monkeypatch):
        """Test raw_request handles read failure on HTTPError."""
        # Line 102-103: OSError during read
        exc = urllib.error.HTTPError(
            "https://example.com", 500, "Server Error", {}, None
        )
        exc.read = Mock(side_effect=OSError("read failed"))
        
        def fake_urlopen(*args, **kwargs):
            raise exc
        
        monkeypatch.setattr("runtime.tools.httpx.urlopen", fake_urlopen)
        status, headers, text, err = raw_request("GET", "https://example.com")
        assert status == 500
        assert text == ""

    def test_raw_request_truncation(self, monkeypatch):
        """Test raw_request truncates large responses."""
        # Line 116-119: truncation
        response = Mock()
        response.status = 200
        response.headers = {}
        response.read = Mock(return_value=b"x" * (httpx.BODY_CAP + 100))
        
        def fake_urlopen(*args, **kwargs):
            return response
        
        with patch("runtime.tools.httpx.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = response
            status, headers, text, err = raw_request("GET", "https://example.com")
            assert "...[truncated]" in text

    def test_raw_request_method_uppercased(self, monkeypatch):
        """Test raw_request uppercases method."""
        # Line 87: method uppercasing
        called_methods = []
        
        def fake_urlopen(req, **kwargs):
            called_methods.append(req.get_method())
            raise urllib.error.URLError("test")
        
        monkeypatch.setattr("runtime.tools.httpx.urlopen", fake_urlopen)
        raw_request("get", "https://example.com")
        assert called_methods[0] == "GET"


class TestGetJSON:
    """Tests for get_json function."""

    def test_get_json_success(self, monkeypatch):
        """Test get_json with valid JSON."""
        # Line 127-135: successful path
        payload = {"key": "value"}
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, json.dumps(payload), "")
        )
        result, err = get_json("https://api.example.com/data")
        assert result == payload
        assert err == ""

    def test_get_json_error(self, monkeypatch):
        """Test get_json with error from raw_request."""
        # Line 128-129
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (0, {}, "", "error: timeout")
        )
        result, err = get_json("https://api.example.com/data")
        assert result is None
        assert err == "error: timeout"

    def test_get_json_http_error(self, monkeypatch):
        """Test get_json with HTTP error status."""
        # Line 130-131
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (404, {}, '{"error":"not found"}', "")
        )
        result, err = get_json("https://api.example.com/data")
        assert result is None
        assert err == "error: HTTP 404"

    def test_get_json_invalid_json(self, monkeypatch):
        """Test get_json with invalid JSON."""
        # Line 132-135: JSONDecodeError
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, "not json", "")
        )
        result, err = get_json("https://api.example.com/data")
        assert result is None
        assert "error:" in err

    def test_get_json_with_custom_headers(self, monkeypatch):
        """Test get_json passes custom headers."""
        # Line 124-126: header passing
        called = []
        
        def fake_raw_request(method, url, **kwargs):
            called.append(kwargs.get("headers", {}))
            return (200, {}, "null", "")
        
        monkeypatch.setattr("runtime.tools.httpx.raw_request", fake_raw_request)
        get_json("https://api.example.com", headers={"X-Custom": "value"})
        assert called
        headers = called[0]
        assert headers.get("Accept") == "application/json"
        assert headers.get("X-Custom") == "value"


class TestPostJSON:
    """Tests for post_json function."""

    def test_post_json_success(self, monkeypatch):
        """Test post_json with successful response."""
        # Line 139-154: successful path
        payload = {"result": "ok"}
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, json.dumps(payload), "")
        )
        result, err = post_json("https://api.example.com/data", {"key": "value"})
        assert result == payload
        assert err == ""

    def test_post_json_error(self, monkeypatch):
        """Test post_json with error."""
        # Line 147-148
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (0, {}, "", "error: timeout")
        )
        result, err = post_json("https://api.example.com/data", {})
        assert result is None
        assert err == "error: timeout"

    def test_post_json_http_error(self, monkeypatch):
        """Test post_json with HTTP error."""
        # Line 149-150: HTTP error with body preview
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (400, {}, "Bad request details", "")
        )
        result, err = post_json("https://api.example.com/data", {})
        assert result is None
        assert "error: HTTP 400" in err
        assert "Bad request" in err

    def test_post_json_invalid_json_response(self, monkeypatch):
        """Test post_json with invalid JSON response."""
        # Line 151-154: JSONDecodeError
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, "not json", "")
        )
        result, err = post_json("https://api.example.com/data", {})
        assert result is None
        assert "error:" in err

    def test_post_json_encoding(self, monkeypatch):
        """Test post_json encodes payload."""
        # Line 139: JSON encoding
        called = []
        
        def fake_raw_request(method, url, **kwargs):
            called.append(kwargs.get("body"))
            return (200, {}, "null", "")
        
        monkeypatch.setattr("runtime.tools.httpx.raw_request", fake_raw_request)
        post_json("https://api.example.com", {"key": "value"})
        assert called
        body = called[0]
        assert b"key" in body
        assert b"value" in body


class TestFetchText:
    """Tests for fetch_text function."""

    def test_fetch_text_success(self, monkeypatch):
        """Test fetch_text with successful response."""
        # Line 158-159: successful path
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, "content here", "")
        )
        result = fetch_text("https://example.com/page.txt")
        assert result == "content here"

    def test_fetch_text_error(self, monkeypatch):
        """Test fetch_text with error."""
        # Line 159-160
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (0, {}, "", "error: timeout")
        )
        result = fetch_text("https://example.com/page.txt")
        assert result == "error: timeout"

    def test_fetch_text_http_error(self, monkeypatch):
        """Test fetch_text with HTTP error."""
        # Line 161-162: HTTP error status
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (500, {}, "", "")
        )
        result = fetch_text("https://example.com/page.txt")
        assert result == "error: HTTP 500"

    def test_fetch_text_empty_response(self, monkeypatch):
        """Test fetch_text with empty response."""
        # Line 163: empty response becomes "(empty)"
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, "", "")
        )
        result = fetch_text("https://example.com/page.txt")
        assert result == "(empty)"


class TestHTTPRequest:
    """Tests for http_request function."""

    def test_http_request_header_parse_error(self):
        """Test http_request with invalid headers."""
        # Line 174-176: header parsing error
        result = http_request("GET", "https://example.com", headers="bad header line")
        assert result.startswith("error:")

    def test_http_request_success(self, monkeypatch):
        """Test http_request with successful response."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {"Content-Type": "text/plain"}, "response body", "")
        )
        result = http_request("GET", "https://example.com")
        assert "status: 200" in result
        assert "Content-Type: text/plain" in result
        assert "response body" in result

    def test_http_request_json_headers(self, monkeypatch):
        """Test http_request with JSON headers."""
        # Line 174-176: JSON header parsing
        called = []
        
        def fake_raw_request(method, url, **kwargs):
            called.append(kwargs.get("headers"))
            return (200, {}, "ok", "")
        
        monkeypatch.setattr("runtime.tools.httpx.raw_request", fake_raw_request)
        http_request(
            "GET",
            "https://example.com",
            headers='{"X-Custom": "value"}'
        )
        assert called[0].get("X-Custom") == "value"

    def test_http_request_header_colon_format(self, monkeypatch):
        """Test http_request with colon-format headers."""
        called = []
        
        def fake_raw_request(method, url, **kwargs):
            called.append(kwargs.get("headers"))
            return (200, {}, "ok", "")
        
        monkeypatch.setattr("runtime.tools.httpx.raw_request", fake_raw_request)
        http_request("GET", "https://example.com", headers="X-Custom: value")
        assert called[0].get("X-Custom") == "value"

    def test_http_request_interesting_headers(self, monkeypatch):
        """Test http_request filters interesting headers."""
        # Line 188-192: interesting header filtering
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (
                200,
                {
                    "Content-Type": "text/html",
                    "Location": "https://redirect.com",
                    "X-Custom": "should-not-appear",
                    "X-Request-ID": "123"
                },
                "body",
                ""
            )
        )
        result = http_request("GET", "https://example.com")
        assert "Content-Type:" in result
        assert "Location:" in result
        assert "X-Request-ID:" in result
        assert "X-Custom" not in result


class TestOpenAPIops:
    """Tests for openapi_ops function."""

    def test_openapi_ops_success(self, monkeypatch):
        """Test openapi_ops with valid OpenAPI spec."""
        # Line 199-228
        spec = {
            "paths": {
                "/users": {
                    "get": {"summary": "List users"},
                    "post": {"summary": "Create user"}
                },
                "/users/{id}": {
                    "get": {"summary": "Get user"}
                }
            }
        }
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, json.dumps(spec), "")
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert "GET /users — List users" in result
        assert "POST /users — Create user" in result
        assert "GET /users/{id} — Get user" in result

    def test_openapi_ops_error(self, monkeypatch):
        """Test openapi_ops error response."""
        # Line 201-202
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (0, {}, "", "error: timeout")
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert result == "error: timeout"

    def test_openapi_ops_http_error(self, monkeypatch):
        """Test openapi_ops with HTTP error."""
        # Line 203-204
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (404, {}, "", "")
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert result == "error: HTTP 404"

    def test_openapi_ops_invalid_json(self, monkeypatch):
        """Test openapi_ops with invalid JSON."""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, "not json", "")
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert result.startswith("error:")

    def test_openapi_ops_no_paths(self, monkeypatch):
        """Test openapi_ops with no paths in spec."""
        # Line 209-210
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, json.dumps({"info": "test"}), "")
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert "error: no paths in OpenAPI document" in result

    def test_openapi_ops_yaml_format(self, monkeypatch):
        """Test openapi_ops with YAML format."""
        # Line 235: _paths_from_yaml fallback
        yaml_spec = """
paths:
  /users:
    get:
    post:
  /items:
    get:
"""
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, yaml_spec, "")
        )
        result = openapi_ops("https://api.example.com/openapi.yaml")
        assert "GET /users" in result
        assert "POST /users" in result

    def test_openapi_ops_truncation(self, monkeypatch):
        """Test openapi_ops truncates at cap."""
        # Line 225-227
        paths = {f"/endpoint{i}": {"get": {}} for i in range(httpx.OPENAPI_CAP + 10)}
        spec = {"paths": paths}
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, json.dumps(spec), "")
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert "...[truncated]" in result

    def test_openapi_ops_with_operation_id(self, monkeypatch):
        """Test openapi_ops uses operationId when no summary."""
        # Line 220: operationId fallback
        spec = {
            "paths": {
                "/users": {
                    "get": {"operationId": "listUsers"}
                }
            }
        }
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, json.dumps(spec), "")
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert "GET /users — listUsers" in result

    def test_openapi_ops_empty_paths(self, monkeypatch):
        """Test openapi_ops with empty paths dict."""
        # Line 228: no operations message
        monkeypatch.setattr(
            "runtime.tools.httpx.raw_request",
            lambda *a, **k: (200, {}, json.dumps({"paths": {}}), "")
        )
        result = openapi_ops("https://api.example.com/openapi.json")
        assert result == "(no operations)"


class TestParseOpenAPI:
    """Tests for _parse_openapi function."""

    def test_parse_openapi_json(self):
        """Test _parse_openapi with JSON."""
        # Line 232-233
        result = _parse_openapi('{"paths": {}}')
        assert isinstance(result, dict)
        assert "paths" in result

    def test_parse_openapi_invalid_json_tries_yaml(self, monkeypatch):
        """Test _parse_openapi falls back to YAML."""
        # Line 234-235
        called = []
        
        def fake_yaml(text):
            called.append(True)
            return {"paths": {}}
        
        monkeypatch.setattr("runtime.tools.httpx._paths_from_yaml", fake_yaml)
        result = _parse_openapi("invalid json {")
        assert called


class TestPathsFromYAML:
    """Tests for _paths_from_yaml function."""

    def test_paths_from_yaml_basic(self):
        """Test _paths_from_yaml with basic YAML."""
        # Line 239-264
        yaml = """
paths:
  /users:
    get:
    post:
  /items:
    get:
"""
        result = _paths_from_yaml(yaml)
        assert "paths" in result
        assert "/users" in result["paths"]
        assert "get" in result["paths"]["/users"]
        assert "post" in result["paths"]["/users"]

    def test_paths_from_yaml_ignores_comments(self):
        """Test _paths_from_yaml ignores comments."""
        # Line 244: comment handling
        yaml = """
# This is a comment
paths:
  # Another comment
  /users: # inline comment
    get:
"""
        result = _paths_from_yaml(yaml)
        assert "/users" in result["paths"]

    def test_paths_from_yaml_no_paths_section(self):
        """Test _paths_from_yaml without paths section."""
        # Line 263
        yaml = "info: test"
        result = _paths_from_yaml(yaml)
        assert isinstance(result, str)
        assert "error:" in result

    def test_paths_from_yaml_stops_at_unindented_line(self):
        """Test _paths_from_yaml stops at unindented line."""
        # Line 253-254
        yaml = """
paths:
  /users:
    get:
info:
  title: API
"""
        result = _paths_from_yaml(yaml)
        assert "/users" in result["paths"]

    def test_paths_from_yaml_invalid_method(self):
        """Test _paths_from_yaml ignores invalid methods."""
        # Line 260
        yaml = """
paths:
  /users:
    invalid:
    get:
"""
        result = _paths_from_yaml(yaml)
        assert "/users" in result["paths"]


class TestParseHeaders:
    """Tests for _parse_headers function."""

    def test_parse_headers_empty(self):
        """Test _parse_headers with empty input."""
        # Line 270-271
        headers, err = _parse_headers("")
        assert headers == {}
        assert err == ""

    def test_parse_headers_json(self):
        """Test _parse_headers with JSON format."""
        # Line 272-279
        headers, err = _parse_headers('{"X-Custom": "value", "Content-Type": "application/json"}')
        assert headers.get("X-Custom") == "value"
        assert headers.get("Content-Type") == "application/json"
        assert err == ""

    def test_parse_headers_invalid_json(self):
        """Test _parse_headers with invalid JSON."""
        # Line 275-276
        headers, err = _parse_headers('{invalid}')
        assert headers == {}
        assert err.startswith("error:")

    def test_parse_headers_json_not_object(self):
        """Test _parse_headers with JSON array instead of object."""
        # Line 277-278
        headers, err = _parse_headers('["not", "an", "object"]')
        assert headers == {}
        assert err == "error: headers JSON must be an object"

    def test_parse_headers_colon_format(self):
        """Test _parse_headers with colon-format headers."""
        # Line 280-286
        headers, err = _parse_headers("X-Custom: value\nContent-Type: application/json")
        assert headers.get("X-Custom") == "value"
        assert headers.get("Content-Type") == "application/json"
        assert err == ""

    def test_parse_headers_colon_format_missing_colon(self):
        """Test _parse_headers with missing colon."""
        # Line 283-284
        headers, err = _parse_headers("X-Custom value")
        assert headers == {}
        assert "error: bad header line:" in err

    def test_parse_headers_colon_format_multiple_colons(self):
        """Test _parse_headers with multiple colons."""
        # Line 285-286: only first colon splits
        headers, err = _parse_headers("X-Custom: value: with: colons")
        assert headers.get("X-Custom") == "value: with: colons"
        assert err == ""

    def test_parse_headers_json_converts_to_strings(self):
        """Test _parse_headers converts JSON values to strings."""
        # Line 279: string conversion
        headers, err = _parse_headers('{"X-Number": 123, "X-Bool": true}')
        assert headers.get("X-Number") == "123"
        assert headers.get("X-Bool") == "True"
        assert err == ""


class TestDecode:
    """Tests for _decode function."""

    def test_decode_utf8(self):
        """Test _decode with valid UTF-8."""
        # Line 291-292
        result = _decode(b"hello world")
        assert result == "hello world"

    def test_decode_invalid_utf8_with_replacement(self):
        """Test _decode with invalid UTF-8 uses replacement."""
        # Line 293-294
        invalid_bytes = b"\x80\x81\x82"
        result = _decode(invalid_bytes)
        assert isinstance(result, str)
        # Should contain replacement characters, not raise
        assert len(result) > 0
