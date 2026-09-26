from __future__ import annotations

import http.client
import ipaddress
import json
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from runtime.tools.web import MAX_FETCH, USER_AGENT

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
SAFE_METHODS = {"GET", "HEAD"}
OPENAPI_CAP = 80
BODY_CAP = 50_000
BLOCKED_HOSTS = {
    "metadata.google.internal",
    "metadata.gce.internal",
    "169.254.169.254",
}

# Explicit redirect cap so loops/too-many-redirects fail predictably and fast
# instead of relying on urllib's default (10 total, 4 repeats of one URL).
MAX_REDIRECTS = 5

# Retry/backoff tuning for transient failures (timeouts, DNS, resets, 5xx).
# Kept small so a single tool call stays bounded (worst case a few seconds).
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5
RETRY_BACKOFF_FACTOR = 2.0
RETRY_BACKOFF_CAP = 4.0
# Only retry methods that are safe to repeat; POST/PATCH are never retried.
IDEMPOTENT_METHODS = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise urllib.error.URLError("redirect must be http or https")
        if blocked_host(parsed.netloc):
            raise urllib.error.URLError("blocked host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(request, timeout=20.0):
    opener = urllib.request.build_opener(_SafeRedirect)
    return opener.open(request, timeout=timeout)


def _retry_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at RETRY_BACKOFF_CAP."""
    delay = min(RETRY_BACKOFF_BASE * (RETRY_BACKOFF_FACTOR**attempt), RETRY_BACKOFF_CAP)
    return delay + random.uniform(0, delay * 0.25)


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


def _redirect_loop_error(exc: urllib.error.HTTPError) -> bool:
    """True if this HTTPError is urllib's redirect-loop/too-many-redirects
    pseudo-error rather than a real 3xx response from the server."""
    return exc.code in (301, 302, 303, 307, 308) and "infinite loop" in str(exc.reason)


def _classify_urlerror(exc: urllib.error.URLError, url: str, host: str, timeout: float) -> str:
    """Turn a URLError (possibly wrapping a lower-level exception in
    .reason) into a clear error string."""
    reason = exc.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"error: TLS certificate verification failed for {host}: {reason}"
    if isinstance(reason, ssl.SSLError):
        return f"error: TLS error for {host}: {reason}"
    if isinstance(reason, socket.gaierror):
        return f"error: could not resolve host {host}"
    if isinstance(reason, TimeoutError):
        return f"error: fetch timed out for {url} after {timeout}s"
    if isinstance(reason, (ConnectionResetError, http.client.RemoteDisconnected)):
        return "error: connection reset by peer"
    return f"error: {reason}"


def _is_retryable_error(err: str) -> bool:
    """True for transport-level failures worth retrying (timeouts, DNS,
    resets). TLS/scheme/blocked-host/redirect-loop errors are not transient."""
    return err.startswith(
        ("error: fetch timed out", "error: could not resolve host", "error: connection reset")
    )


def raw_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 20.0,
    cap: int = MAX_FETCH,
) -> tuple[int, dict[str, str], str, str]:
    """Return (status, headers, text, error). error is set on failure.

    Idempotent methods (see IDEMPOTENT_METHODS) are retried with capped
    exponential backoff on transient transport failures and on 5xx
    responses. Non-idempotent methods (POST, PATCH) and 4xx responses are
    never retried. A final 5xx is still returned as (status, headers,
    text, "") — retries are an internal detail, not a change to the
    status>=400 contract that get_json/post_json/fetch_text rely on.
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
    host = parsed.hostname or _hostname(parsed.netloc)
    retryable_method = method in IDEMPOTENT_METHODS
    max_attempts = (MAX_RETRIES + 1) if retryable_method else 1

    for attempt in range(max_attempts):
        last_attempt = attempt == max_attempts - 1
        request = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
                resp_headers = {k: v for k, v in response.headers.items()}
                raw = b"" if method == "HEAD" else response.read(cap + 1)
        except urllib.error.HTTPError as exc:
            if _redirect_loop_error(exc):
                return 0, {}, "", f"error: too many redirects for {url}"
            try:
                raw = exc.read(cap + 1) if method != "HEAD" else b""
            except OSError:
                raw = b""
            status = int(exc.code)
            resp_headers = {k: v for k, v in (exc.headers.items() if exc.headers else [])}
            text = _decode(raw[:cap])
            if len(raw) > cap:
                text += "\n...[truncated]"
            if 500 <= status < 600 and retryable_method and not last_attempt:
                time.sleep(_retry_delay(attempt))
                continue
            return status, resp_headers, text, ""
        except ssl.SSLCertVerificationError as exc:
            return 0, {}, "", f"error: TLS certificate verification failed for {host}: {exc}"
        except ssl.SSLError as exc:
            return 0, {}, "", f"error: TLS error for {host}: {exc}"
        except socket.gaierror:
            err = f"error: could not resolve host {host}"
            if retryable_method and not last_attempt:
                time.sleep(_retry_delay(attempt))
                continue
            return 0, {}, "", err
        except (ConnectionResetError, http.client.RemoteDisconnected):
            err = "error: connection reset by peer"
            if retryable_method and not last_attempt:
                time.sleep(_retry_delay(attempt))
                continue
            return 0, {}, "", err
        except TimeoutError:
            err = f"error: fetch timed out for {url} after {timeout}s"
            if retryable_method and not last_attempt:
                time.sleep(_retry_delay(attempt))
                continue
            return 0, {}, "", err
        except urllib.error.URLError as exc:
            err = _classify_urlerror(exc, url, host, timeout)
            if _is_retryable_error(err) and retryable_method and not last_attempt:
                time.sleep(_retry_delay(attempt))
                continue
            return 0, {}, "", err
        except OSError as exc:
            return 0, {}, "", f"error: {exc}"
        else:
            truncated = len(raw) > cap
            text = _decode(raw[:cap])
            if truncated:
                text += "\n...[truncated]"
            if 500 <= status < 600 and retryable_method and not last_attempt:
                time.sleep(_retry_delay(attempt))
                continue
            return status, resp_headers, text, ""
    # Unreachable: the loop always returns or continues, and the last
    # iteration never continues.
    return 0, {}, "", "error: request failed"


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
