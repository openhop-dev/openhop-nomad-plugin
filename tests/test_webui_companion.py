"""Offline Chromium settings actions and accessible tab regression."""

import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_tabs_and_companion_controls(tmp_path):
    sync = pytest.importorskip("playwright.sync_api")
    config = json.loads((ROOT / "config.default.json").read_text())
    config.update(
        future={"keep": True},
        _runtime={"ignore": True},
        max_concurrent_requests=3,
        max_requests_per_sender=7,
    )
    config.pop("max_pending_requests")
    config.pop("max_requests_global")
    posts, errors = [], []
    values = {"auto_add": "selected", "overwrite_oldest": False, "path_hash_bytes": 2}
    runtime = {"token": "a" * 64, "connected": True, "endpoint": "running:5050", "result": None}
    pending = None
    with sync.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(r):
            nonlocal config, pending
            url = r.request.url
            assert url.startswith("http://nomad.test/")
            if "/api/plugins/runtime" in url:
                runtime["updated_at"] = time.time()
                return r.fulfill(json={"runtime": {"companion": runtime}})
            if "/api/plugins/settings" in url:
                if r.request.method == "POST":
                    payload = r.request.post_data_json
                    posts.append(payload)
                    config = payload["config"]
                    if "companion_request" in config:
                        pending = config["companion_request"]
                return r.fulfill(json={"config": config})
            assert "/api/" not in url
            return r.fulfill(
                path=str(ROOT / "ui" / (url.split("/plugins/openhop.nomad/")[-1] or "index.html"))
            )

        page.route("**/*", route)
        page.goto("http://nomad.test/plugins/openhop.nomad/")
        page.wait_for_function(
            "document.querySelector('#global-status').textContent.includes('Ready')"
        )
        assert page.get_by_role("tab").count() == 5
        page.locator("#nomad_model").fill("unsaved-model")
        page.get_by_role("tab", name="Connectivity", exact=True).press("ArrowRight")
        assert (
            page.get_by_role("tab", name="Behavior & access").get_attribute("aria-selected")
            == "true"
        )
        page.locator("#one_shot").check()
        page.get_by_role("tab", name="Behavior & access").press("ArrowRight")
        page.get_by_role("tab", name="Replies").press("ArrowRight")
        assert (
            page.get_by_role("tab", name="Companion device").get_attribute("aria-selected")
            == "true"
        )
        page.wait_for_function("!document.querySelector('#companion-read').disabled")
        assert page.locator("#companion-apply").is_disabled()
        assert not posts
        page.locator("#companion-read").click()
        page.wait_for_function(
            "document.querySelector('#companion-status').textContent.includes('Waiting')"
        )
        assert pending["patch"] == {}
        assert posts[-1]["restart"] is False
        assert posts[-1]["config"]["nomad_model"] != "unsaved-model"
        assert posts[-1]["config"]["future"] == {"keep": True}
        assert "_runtime" not in posts[-1]["config"]
        assert page.locator("#companion-apply").is_disabled()
        runtime["result"] = {"id": pending["id"], "status": "verified", "values": values.copy()}
        runtime["token"] = "b" * 64
        page.wait_for_function("!document.querySelector('#companion-apply').disabled")
        assert page.locator("#companion-auto-add").input_value() == ""
        assert page.locator("#companion-path-bytes").input_value() == "2"
        # Existing selected type policy is preserved unless All/None chosen.
        for auto, oldest, size in [(None, True, "1"), ("none", False, "3"), ("all", True, "2")]:
            if auto:
                page.locator("#companion-auto-add").select_option(auto)
            page.locator("#companion-overwrite").set_checked(oldest)
            page.locator("#companion-path-bytes").select_option(size)
            page.locator("#companion-apply").click()
            page.wait_for_function(
                "document.querySelector('#companion-status').textContent.includes('Waiting')"
            )
            patch = pending["patch"]
            assert patch.get("auto_add") == auto
            assert patch["overwrite_oldest"] is oldest
            assert patch["path_hash_bytes"] == int(size)
            assert posts[-1]["restart"] is False
            assert page.locator("#companion-read").is_disabled()
            values.update(patch)
            runtime["result"] = {"id": pending["id"], "status": "verified", "values": values.copy()}
            runtime["token"] = ("c" if oldest else "d") * 64
            page.wait_for_function(
                "document.querySelector('#companion-status').textContent.includes('read back')"
            )
        page.get_by_role("tab", name="Companion device").press("Home")
        assert page.locator("#nomad_model").input_value() == "unsaved-model"
        page.get_by_role("tab", name="Replies", exact=True).click()
        page.locator("#max_chunk_bytes").fill("91")
        page.locator("button[type=submit]").click()
        page.wait_for_function(
            "document.querySelector('#global-status').textContent.includes('Saved')"
        )
        assert posts[-1]["restart"] is True
        assert posts[-1]["config"]["nomad_model"] == "unsaved-model"
        assert posts[-1]["config"]["one_shot"] is True
        assert posts[-1]["config"]["max_chunk_bytes"] == 91
        assert posts[-1]["config"]["max_pending_requests"] == 3
        assert posts[-1]["config"]["max_requests_global"] == 7
        assert "companion_request" not in posts[-1]["config"]
        page.reload()
        page.wait_for_function(
            "document.querySelector('#global-status').textContent.includes('Ready')"
        )
        assert page.locator("#nomad_model").input_value() == "unsaved-model"
        assert page.locator("#one_shot").is_checked()
        assert page.locator("#max_chunk_bytes").input_value() == "91"
        for theme in ("light", "dark"):
            page.emulate_media(color_scheme=theme)
            for width in (390, 768, 1280):
                page.set_viewport_size({"width": width, "height": 900})
                for tab in page.get_by_role("tab").all():
                    tab.click()
                    assert page.get_by_role("tabpanel").count() == 1
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                # Navigate back to companion tab for layout checks.
                page.get_by_role("tab", name="Companion device", exact=True).click()
                # Labels and controls share a consistent row, including the checkbox.
                auto = page.locator("#companion-auto-add").bounding_box()
                overwrite = page.locator("#panel-companion .check-card").bounding_box()
                path = page.locator("#companion-path-bytes").bounding_box()
                assert abs(auto["height"] - overwrite["height"]) <= 1
                if width > 650:
                    assert abs(auto["y"] - overwrite["y"]) <= 1
                if width > 940:
                    assert abs(auto["y"] - path["y"]) <= 1
                page.screenshot(
                    path=str(tmp_path / f"companion-{theme}-{width}.png"), full_page=True
                )
        runtime["connected"] = False
        page.reload()
        page.get_by_role("tab", name="Companion device").click()
        page.wait_for_timeout(300)
        assert page.locator("#companion-read").is_disabled()
        assert page.locator("#companion-apply").is_disabled()
        assert not errors
        browser.close()
