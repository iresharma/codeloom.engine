from __future__ import annotations

import fnmatch


def write_allowed(rel: str, globs: list[str] | None) -> bool:
    """None = any path; empty list = none; otherwise match any glob."""
    if globs is None:
        return True
    if not globs:
        return False
    rel = rel.replace("\\", "/").lstrip("./")
    name = rel.rsplit("/", 1)[-1]
    for pattern in globs:
        pat = pattern.replace("\\", "/")
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
        if pat.startswith("**/") and pat.endswith("/**"):
            mid = pat[3:-3]
            if not mid:
                return True
            if rel == mid or rel.startswith(mid + "/") or f"/{mid}/" in f"/{rel}/":
                return True
    return False
