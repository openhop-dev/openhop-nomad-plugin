"""Offline browser manual controls; mocked manager API only."""

import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_manual_actions_use_existing_settings_runtime_only():
    sync = pytest.importorskip("playwright.sync_api")
    config = json.loads((ROOT / "config.default.json").read_text())
    config["future"] = 42
    posts = []
    runtime = {"token": "a" * 64, "connected": True, "endpoint": "saved:5050", "result": None}
    with sync.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(r):
            nonlocal config
            url = r.request.url
            assert url.startswith("http://nomad.test/")
            if "/api/plugins/runtime" in url:
                runtime["updated_at"] = time.time()
                return r.fulfill(json={"runtime": {"advert": runtime}})
            if "/api/plugins/settings" in url:
                if r.request.method == "POST":
                    payload = r.request.post_data_json
                    posts.append(payload)
                    config = payload["config"]
                return r.fulfill(json={"config": config})
            assert "/api/" not in url
            return r.fulfill(
                path=str(ROOT / "ui" / (url.split("/plugins/openhop.nomad/")[-1] or "index.html"))
            )

        page.route("**/*", route)
        page.goto("http://nomad.test/plugins/openhop.nomad/")
        page.wait_for_function(
            "document.querySelector('#advert-zero') && !document.querySelector('#advert-zero').disabled"
        )
        assert not posts
        page.locator("#meshcore_host").fill("UNSAVED")
        for mode, button in [("zero-hop", "advert-zero"), ("flood", "advert-flood")]:
            page.locator("#" + button).click()
            page.wait_for_function(
                "document.querySelector('#advert-status').textContent.includes('Waiting')"
            )
            assert page.locator("#advert-zero").is_disabled()
            assert page.locator("#advert-flood").is_disabled()
            payload = posts[-1]
            assert payload["restart"] is False
            assert payload["config"]["meshcore_host"] == "127.0.0.1"
            assert payload["config"]["future"] == 42
            request = payload["config"]["advert_request"]
            assert request["mode"] == mode
            runtime["token"] = "b" * 64
            runtime["result"] = {"id": request["id"], "status": "accepted"}
            page.wait_for_function(
                "document.querySelector('#advert-status').textContent.includes('RF delivery is not confirmed')"
            )
        assert len(posts) == 2
        runtime["result"] = {"id": "other", "status": "pending"}
        page.reload()
        page.wait_for_timeout(300)
        assert page.locator("#advert-zero").is_disabled()
        runtime["result"] = None
        runtime["token"] = None
        page.reload()
        page.wait_for_timeout(300)
        assert page.locator("#advert-zero").is_disabled()
        assert not errors
        browser.close()
