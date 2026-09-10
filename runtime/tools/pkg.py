from __future__ import annotations

import urllib.parse

from runtime.tools.httpx import get_json

ECOSYSTEMS = ("pypi", "npm", "crates", "go", "maven", "nuget", "rubygems")
DESC_CAP = 800


def pkg_info(ecosystem: str, name: str) -> str:
    eco = (ecosystem or "").strip().lower()
    name = (name or "").strip()
    if eco not in ECOSYSTEMS:
        return f"error: ecosystem must be one of {', '.join(ECOSYSTEMS)}"
    if not name:
        return "error: name is required"
    fetchers = {
        "pypi": _pypi,
        "npm": _npm,
        "crates": _crates,
        "go": _go,
        "maven": _maven,
        "nuget": _nuget,
        "rubygems": _rubygems,
    }
    return fetchers[eco](name)


def _pypi(name: str) -> str:
    payload, err = get_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    if err:
        return err
    info = (payload or {}).get("info") or {}
    return _block(
        name=info.get("name") or name,
        version=info.get("version"),
        summary=info.get("summary"),
        homepage=info.get("home_page") or info.get("project_url"),
        license_name=info.get("license"),
        yanked=bool(info.get("yanked")),
    )


def _npm(name: str) -> str:
    encoded = urllib.parse.quote(name, safe="@/")
    payload, err = get_json(f"https://registry.npmjs.org/{encoded}")
    if err:
        return err
    dist = (payload or {}).get("dist-tags") or {}
    latest = dist.get("latest") or ""
    versions = (payload or {}).get("versions") or {}
    meta = versions.get(latest) or payload or {}
    deprecated = meta.get("deprecated") or ""
    return _block(
        name=(payload or {}).get("name") or name,
        version=latest,
        summary=meta.get("description") or (payload or {}).get("description"),
        homepage=meta.get("homepage") or (payload or {}).get("homepage"),
        license_name=_license(meta.get("license") or (payload or {}).get("license")),
        yanked=bool(deprecated),
        extra=f"deprecated: {deprecated}" if deprecated else "",
    )


def _crates(name: str) -> str:
    payload, err = get_json(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}")
    if err:
        return err
    crate = (payload or {}).get("crate") or {}
    newest = (payload or {}).get("versions") or []
    yanked = bool(newest and newest[0].get("yanked"))
    return _block(
        name=crate.get("name") or name,
        version=crate.get("max_stable_version") or crate.get("max_version"),
        summary=crate.get("description"),
        homepage=crate.get("homepage") or crate.get("repository") or crate.get("documentation"),
        license_name=crate.get("license"),
        yanked=yanked,
    )


def _go(name: str) -> str:
    encoded = urllib.parse.quote(name, safe="/")
    payload, err = get_json(f"https://proxy.golang.org/{encoded}/@latest")
    if err:
        return err
    version = (payload or {}).get("Version") or ""
    return _block(
        name=name,
        version=version,
        summary="",
        homepage=f"https://pkg.go.dev/{name}",
        license_name="",
        yanked=False,
    )


def _maven(name: str) -> str:
    if ":" in name:
        group, artifact = name.split(":", 1)
        query = f'g:"{group}" AND a:"{artifact}"'
    else:
        query = name
    params = urllib.parse.urlencode({"q": query, "rows": "1", "wt": "json"})
    payload, err = get_json("https://search.maven.org/solrsearch/select?" + params)
    if err:
        return err
    docs = ((payload or {}).get("response") or {}).get("docs") or []
    if not docs:
        return "(no results)"
    doc = docs[0]
    coord = f"{doc.get('g')}:{doc.get('a')}"
    return _block(
        name=coord,
        version=doc.get("latestVersion"),
        summary="",
        homepage=f"https://search.maven.org/artifact/{doc.get('g')}/{doc.get('a')}",
        license_name="",
        yanked=False,
    )


def _nuget(name: str) -> str:
    params = urllib.parse.urlencode({"q": name, "take": "1"})
    payload, err = get_json("https://azuresearch-usnc.nuget.org/query?" + params)
    if err:
        return err
    data = (payload or {}).get("data") or []
    if not data:
        return "(no results)"
    item = data[0]
    versions = item.get("versions") or []
    latest = versions[-1].get("version") if versions else item.get("version")
    return _block(
        name=item.get("id") or name,
        version=latest,
        summary=item.get("description"),
        homepage=item.get("projectUrl"),
        license_name=item.get("licenseUrl"),
        yanked=False,
    )


def _rubygems(name: str) -> str:
    payload, err = get_json(f"https://rubygems.org/api/v1/gems/{urllib.parse.quote(name)}.json")
    if err:
        return err
    return _block(
        name=(payload or {}).get("name") or name,
        version=(payload or {}).get("version"),
        summary=(payload or {}).get("info"),
        homepage=(payload or {}).get("homepage_uri") or (payload or {}).get("project_uri"),
        license_name=", ".join((payload or {}).get("licenses") or [])
        or (payload or {}).get("licenses"),
        yanked=False,
    )


def _license(value) -> str:
    if isinstance(value, dict):
        return str(value.get("type") or value.get("name") or "")
    return str(value or "")


def _block(
    *,
    name: str,
    version,
    summary,
    homepage,
    license_name,
    yanked: bool,
    extra: str = "",
) -> str:
    summary = _clip(str(summary or "").strip(), DESC_CAP)
    lines = [
        f"name: {name}",
        f"version: {version or '(unknown)'}",
        f"summary: {summary or '(none)'}",
        f"homepage: {homepage or '(none)'}",
        f"license: {license_name or '(unknown)'}",
        f"yanked: {yanked}",
    ]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _clip(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + "..."
