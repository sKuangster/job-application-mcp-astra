"""Explicit opt-in local-stack smoke; public query only, no paid providers or applications."""
import os

import pytest

from applypilot.config import PerplexicaSettings
from applypilot.evidence import PublicFetcher
from applypilot.perplexica import PerplexicaAdapter


@pytest.mark.skipif(os.environ.get("APPLYPILOT_LIVE_RESEARCH") != "1", reason="local research stack smoke requires explicit opt-in")
async def test_live_perplexica_searxng_ollama():
    settings = PerplexicaSettings(base_url=os.environ.get("APPLYPILOT_PERPLEXICA_URL", "http://127.0.0.1:3001"))
    adapter = PerplexicaAdapter(settings)
    assert (await adapter.health())["state"] == "ready"
    sources = await adapter.search("Mozilla Firefox engineering mission, browser privacy and internship opportunities. Prefer mozilla.org original pages.")
    assert sources
    inspected = await PublicFetcher().fetch(sources[0]["url"])
    assert inspected["inspected"] and inspected["quotes"]
