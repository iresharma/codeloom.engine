from __future__ import annotations

from runtime.tools.httpx import post_json

ECOSYSTEMS = {
    "pypi": "PyPI",
    "npm": "npm",
    "crates": "crates.io",
    "go": "Go",
    "maven": "Maven",
    "nuget": "NuGet",
    "rubygems": "RubyGems",
}
VULN_CAP = 10
SUMMARY_CAP = 400


def osv_query(ecosystem: str, package: str, *, version: str = "") -> str:
    eco = ECOSYSTEMS.get((ecosystem or "").strip().lower())
    package = (package or "").strip()
    if eco is None:
        return f"error: ecosystem must be one of {', '.join(sorted(ECOSYSTEMS))}"
    if not package:
        return "error: package is required"
    payload = {"package": {"name": package, "ecosystem": eco}}
    if version.strip():
        payload["version"] = version.strip()
    data, err = post_json("https://api.osv.dev/v1/query", payload)
    if err:
        return err
    vulns = (data or {}).get("vulns") or []
    if not vulns:
        return "(no advisories)"
    blocks = []
    for item in vulns[:VULN_CAP]:
        aliases = ", ".join(item.get("aliases") or []) or "(none)"
        severity = _severity(item)
        ranges = _ranges(item)
        summary = (item.get("summary") or item.get("details") or "").strip()
        if len(summary) > SUMMARY_CAP:
            summary = summary[:SUMMARY_CAP] + "..."
        blocks.append(
            "\n".join(
                [
                    f"id: {item.get('id') or ''}",
                    f"summary: {summary or '(none)'}",
                    f"aliases: {aliases}",
                    f"severity: {severity}",
                    f"affected: {ranges}",
                ]
            )
        )
    extra = ""
    if len(vulns) > VULN_CAP:
        extra = f"\n\n...({len(vulns) - VULN_CAP} more)"
    return "\n\n".join(blocks) + extra


def _severity(item: dict) -> str:
    for entry in item.get("severity") or []:
        if isinstance(entry, dict) and entry.get("score"):
            return str(entry.get("score"))
    db = item.get("database_specific") or {}
    if isinstance(db, dict) and db.get("severity"):
        return str(db.get("severity"))
    return "(unknown)"


def _ranges(item: dict) -> str:
    parts = []
    for affected in item.get("affected") or []:
        pkg = (affected.get("package") or {}).get("name") or ""
        for rng in affected.get("ranges") or []:
            events = rng.get("events") or []
            bits = []
            for event in events:
                if event.get("introduced"):
                    bits.append(f">={event['introduced']}")
                if event.get("fixed"):
                    bits.append(f"<{event['fixed']}")
            if bits:
                parts.append(f"{pkg} {' '.join(bits)}".strip())
    return "; ".join(parts) or "(unspecified)"
