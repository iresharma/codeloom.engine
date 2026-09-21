from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from runtime.tools.web import MAX_FETCH, USER_AGENT

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
SAFE_METHODS = {"GET", "HEAD"}
# Methods considered idempotent per HTTP semantics (safe to retry). OPTIONS is
# idempotent but not among the methods this tool exposes; kept for clarity.
IDEMPOTENT_METHODS = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}
OPENAPI_CAP = 80
BODY_CAP = 50_000
BLOCKED_HOSTS = {
    "metadata.google.internal",
    "metadata.gce.internal",
    "169.254.169.254",
}
# Explicit redirect-hop cap. urllib's own default (max_redirections=10, plus a
# max_repeats=4 same-url check) only raises an obscure HTTPError that our old
# code mistook for a real response. We enforce our own, lower cap and raise a
# plain URLError so raw_request() can turn it into "error: too many redirects".
MAX_REDIRECTS = 5
DEFAULT_MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 0.5


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirects = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        depth = getattr(req, "_safe_redirect_depth", 0) + 1
        if depth > self.max_redirects:
            raise urllib.error.URLError("too many redirects")
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise urllib.error.URLError("redirect must be http or https")
        if blocked_host(parsed.netloc):
            raise urllib.error.URLError("blocked host")
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            new_req._safe_redirect_depth = depth
        return new_req


def urlopen(request, timeout=20.0, max_redirects: int = MAX_REDIRECTS):
    handler = _SafeRedirect()
    handler.max_redirects = max_redirects
    opener = urllib.request.build_opener(handler)
    return opener.open(request, timeout=timeout)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def blocked_host(netloc: str) -> bool:
    """True for cloud metadata / link-local. Loopback and LAN stay allowed."""
    host = _hostname(netloc)
    if not host or host in BLOCKED_HOSTS:
        return bool(host)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return False
        return any(_ip_blocked(ipaddress.ip_address(info[4][0])) for info in infos)
    return _ip_blocked(ip)


def _hostname(netloc: str) -> str:
    host = (netloc or "").strip()
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if host.startswith("["):
        end = host.find("]")
        return host[1:end].lower() if end != -1 else host.lower()
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.lower()


def _ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    if ip.is_loopback:
        return False
    return bool(ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved)


def _classify_url_error(exc: urllib.error.URLError) -> str:
    """Turn a urllib URLError (which wraps the real cause in .reason) into a
    specific "error: ..." message. Falls back to a generic connection error.
    """
    reason = exc.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"error: TLS certificate verification failed: {reason}"
    if isinstance(reason, ssl.SSLError):
        return f"error: TLS error: {reason}"
    if isinstance(reason, socket.gaierror):
        return f"error: DNS resolution failed: {reason}"
    if isinstance(reason, TimeoutError):
        return "error: request timed out"
    if isinstance(reason, ConnectionResetError):
        return "error: connection reset"
    if str(reason) == "too many redirects":
        return "error: too many redirects"
    return f"error: {reason}"


def _do_request(
    method: str,
    url: str,
    hdrs: dict[str, str],
    body: bytes | None,
    timeout: float,
    cap: int,
) -> tuple[int, dict[str, str], str, str]:
    """One attempt at the request. Returns (status, headers, text, error)."""
    request = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            resp_headers = {k: v for k, v in response.headers.items()}
            raw = b"" if method == "HEAD" else response.read(cap + 1)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(cap + 1) if method != "HEAD" else b""
        except OSError:
            raw = b""
        status = int(exc.code)
        resp_headers = {k: v for k, v in (exc.headers.items() if exc.headers else [])}
        text = _decode(raw[:cap])
        if len(raw) > cap:
            text += "\n...[truncated]"
        return status, resp_headers, text, ""
    except ssl.SSLCertVerificationError as exc:
        return 0, {}, "", f"error: TLS certificate verification failed: {exc}"
    except ssl.SSLError as exc:
        return 0, {}, "", f"error: TLS error: {exc}"
    except socket.gaierror as exc:
        return 0, {}, "", f"error: DNS resolution failed: {exc}"
    except ConnectionResetError:
        return 0, {}, "", "error: connection reset"
    except urllib.error.URLError as exc:
        return 0, {}, "", _classify_url_error(exc)
    except TimeoutError:
        return 0, {}, "", f"error: request timed out after {timeout:g}s"
    except OSError as exc:
        return 0, {}, "", f"error: {exc}"
    truncated = len(raw) > cap
    text = _decode(raw[:cap])
    if truncated:
        text += "\n...[truncated]"
    return status, resp_headers, text, ""


def raw_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 20.0,
    cap: int = MAX_FETCH,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep_fn=None,
) -> tuple[int, dict[str, str], str, str]:
    """Return (status, headers, text, error). error is set on failure.

    Transient failures (timeouts and 5xx responses) are retried with
    exponential backoff, but only for idempotent methods (see
    IDEMPOTENT_METHODS). 4xx responses and non-idempotent methods (e.g. POST,
    PATCH) are never retried.

    Note: timeout is a single value applied by urllib to the whole request
    (connect + read combined); stdlib urllib does not support splitting
    connect-timeout from read-timeout, so both are governed by this one
    knob.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return 0, {}, "", "error: url must be http or https"
    if blocked_host(parsed.netloc):
        return 0, {}, "", "error: blocked host"
    method = (method or "GET").upper()
    if method not in METHODS:
        return 0, {}, "", f"error: method must be one of {', '.join(METHODS)}"
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    sleep = sleep_fn or _sleep
    retryable = method in IDEMPOTENT_METHODS
    attempts = max(1, max_retries + 1) if retryable else 1

    status = 0
    resp_headers: dict[str, str] = {}
    text = ""
    err = ""
    for attempt in range(attempts):
        status, resp_headers, text, err = _do_request(method, url, hdrs, body, timeout, cap)
        is_timeout = err.startswith("error: request timed out") or err == "error: fetch timed out"
        is_server_error = not err and status >= 500
        transient = retryable and (is_timeout or is_server_error)
        if not transient or attempt == attempts - 1:
            break
        sleep(RETRY_BACKOFF_BASE * (2**attempt))
    return status, resp_headers, text, err


def get_json(url: str, *, timeout: float = 20.0, headers: dict[str, str] | None = None):
    extra = {"Accept": "application/json"}
    if headers:
        extra.update(headers)
    status, _hdrs, text, err = raw_request("GET", url, headers=extra, timeout=timeout)
    if err:
        return None, err
    if status >= 400:
        return None, f"error: HTTP {status}"
    try:
        return json.loads(text or "null"), ""
    except json.JSONDecodeError as exc:
        return None, f"error: {exc}"


def post_json(url: str, payload: dict, *, timeout: float = 20.0):
    body = json.dumps(payload).encode("utf-8")
    status, _hdrs, text, err = raw_request(
        "POST",
        url,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        body=body,
        timeout=timeout,
    )
    if err:
        return None, err
    if status >= 400:
        return None, f"error: HTTP {status} {text[:300]}"
    try:
        return json.loads(text or "null"), ""
    except json.JSONDecodeError as exc:
        return None, f"error: {exc}"


def fetch_text(url: str, *, timeout: float = 20.0, cap: int = MAX_FETCH) -> str:
    status, _hdrs, text, err = raw_request("GET", url, timeout=timeout, cap=cap)
    if err:
        return err
    if status >= 400:
        return f"error: HTTP {status}"
    return text or "(empty)"


def http_request(
    method: str,
    url: str,
    *,
    headers: str = "",
    body: str = "",
    timeout: float = 20.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> str:
    parsed_headers, herr = _parse_headers(headers)
    if herr:
        return herr
    data = body.encode("utf-8") if body else None
    status, resp_headers, text, err = raw_request(
        method,
        url,
        headers=parsed_headers or None,
        body=data,
        timeout=float(timeout or 20.0),
        cap=BODY_CAP,
        max_retries=max_retries,
    )
    if err:
        return err
    interesting = ("content-type", "location", "x-request-id", "allow")
    hdr_lines = []
    for key, value in resp_headers.items():
        if key.lower() in interesting:
            hdr_lines.append(f"{key}: {value}")
    head = f"status: {status}"
    if hdr_lines:
        head += "\n" + "\n".join(hdr_lines)
    return f"{head}\n\n{text}".rstrip() or head


def openapi_ops(url: str) -> str:
    status, _hdrs, text, err = raw_request("GET", url, timeout=20.0, cap=BODY_CAP)
    if err:
        return err
    if status >= 400:
        return f"error: HTTP {status}"
    payload = _parse_openapi(text)
    if isinstance(payload, str):
        return payload
    paths = payload.get("paths") if isinstance(payload, dict) else None
    if not isinstance(paths, dict):
        return "error: no paths in OpenAPI document"
    lines: list[str] = []
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, spec in methods.items():
            if method.lower() not in {m.lower() for m in METHODS} and method.lower() != "head":
                continue
            summary = ""
            if isinstance(spec, dict):
                summary = spec.get("summary") or spec.get("operationId") or ""
            line = f"{method.upper()} {path}"
            if summary:
                line += f" — {summary}"
            lines.append(line)
            if len(lines) >= OPENAPI_CAP:
                lines.append("...[truncated]")
                return "\n".join(lines)
    return "\n".join(lines) if lines else "(no operations)"


def _parse_openapi(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _paths_from_yaml(text)


def _paths_from_yaml(text: str):
    paths: dict[str, dict] = {}
    in_paths = False
    current_path = ""
    path_indent = 0
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if not in_paths:
            if stripped.rstrip(":") == "paths":
                in_paths = True
                path_indent = indent
            continue
        if indent <= path_indent and stripped.rstrip(":") != "paths":
            break
        if stripped.startswith("/") and stripped.endswith(":"):
            current_path = stripped[:-1]
            paths[current_path] = {}
            continue
        method = stripped.rstrip(":").lower()
        if current_path and method in {m.lower() for m in METHODS}:
            paths[current_path][method] = {}
    if not paths:
        return "error: could not parse OpenAPI document"
    return {"paths": paths}


def _parse_headers(raw: str) -> tuple[dict[str, str], str]:
    headers: dict[str, str] = {}
    text = (raw or "").strip()
    if not text:
        return headers, ""
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return {}, f"error: {exc}"
        if not isinstance(payload, dict):
            return {}, "error: headers JSON must be an object"
        return {str(k): str(v) for k, v in payload.items()}, ""
    for line in text.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            return {}, f"error: bad header line: {line}"
        key, value = line.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers, ""


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")
