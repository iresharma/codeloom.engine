import json
from types import SimpleNamespace

import pytest

from runtime.tools import research_gateway
from tools.browser import browser_screenshot


def test_research_transport_is_opt_in(monkeypatch):
    monkeypatch.delenv("ENGINE_RESEARCH_GATEWAY", raising=False)
    assert research_gateway.request("search", {"query": "example"}) is None


def test_research_transport_uses_only_scoped_token(monkeypatch):
    monkeypatch.setenv("ENGINE_RESEARCH_GATEWAY", "http://relay:8080/research")
    monkeypatch.setenv("ENGINE_RESEARCH_TOKEN", "trial-only")
    observed = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return json.dumps({"text": "Pinned source"}).encode()

    def open_request(request, timeout):
        observed.append(request)
        return Response()

    monkeypatch.setattr(research_gateway.urllib.request, "urlopen", open_request)
    assert research_gateway.request("github", {"args": ["api", "repos/example/repo"]}) == "Pinned source"
    assert observed[0].get_header("Authorization") == "Bearer trial-only"
    assert json.loads(observed[0].data)["operation"] == "github"


@pytest.mark.asyncio
async def test_artifact_only_screenshot_omits_images_but_keeps_saved_path(monkeypatch, tmp_path):
    from tools import browser
    from runtime.tools.browser import ScreenshotResult

    async def shot(*args, **kwargs):
        return ScreenshotResult("saved .engine/debug/tester/page.jpg", b"jpeg", b"jpeg")

    monkeypatch.setenv("ENGINE_TOOL_IMAGE_MODE", "artifact-only")
    monkeypatch.setattr(browser.impl, "browser_screenshot", shot)
    result = await browser_screenshot(SimpleNamespace(workspace=tmp_path, agent_id="tester"))
    assert result == "saved .engine/debug/tester/page.jpg"
