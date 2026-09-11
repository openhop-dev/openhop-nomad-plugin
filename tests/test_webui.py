"""Offline browser contract; install playwright + chromium to run locally."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_offline_logo_is_packaged():
    from test_plugin_package import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assets = project["tool"]["setuptools"]["data-files"]
    assert (
        "ui/assets/project_nomad_logo.webp"
        in assets["share/openhop/plugins/openhop.nomad/ui/assets"]
    )
    assert (ROOT / "ui/assets/project_nomad_logo.webp").read_bytes()[:4] == b"RIFF"


def test_browser_config_help_and_offline_assets(tmp_path):
    sync = pytest.importorskip("playwright.sync_api")
    config = json.loads((ROOT / "config.default.json").read_text())
    config.update(future_option={"keep": True}, _runtime={"ignore": True})
    posts, external, errors = [], [], []
    with sync.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(request):
            nonlocal config
            url = request.request.url
            if not url.startswith("http://nomad.test/"):
                external.append(url)
                return request.abort()
            if "/api/plugins/settings" in url:
                if request.request.method == "POST":
                    payload = request.request.post_data_json
                    posts.append(payload)
                    config = payload["config"]
                return request.fulfill(json={"config": config})
            relative = url.split("/plugins/openhop.nomad/", 1)[-1] or "index.html"
            file = ROOT / "ui" / relative
            request.fulfill(path=str(file))

        page.route("**/*", route)
        page.goto("http://nomad.test/plugins/openhop.nomad/")
        page.wait_for_function(
            "document.querySelector('#global-status').textContent.includes('Ready')"
        )
        assert page.locator(".brand > img.nomad-logo").count() == 1
        assert page.locator(".hero img, .brand-mark").count() == 0
        assert page.locator('img[alt="Project N.O.M.A.D."]').evaluate(
            "(img) => img.complete && img.naturalWidth > 0"
        )
        expected = {
            "one_shot",
            "radio_prompt_enabled",
            "max_concurrent_requests",
            "max_pending_requests",
            "max_requests_per_sender",
            "max_requests_global",
            "rate_limit_window_seconds",
            "allowed_sender_prefixes",
            "busy_wait_seconds",
            "duplicate_ttl_seconds",
            "log_level",
            "radio_prompt_template",
            "max_reply_chunks",
            "max_chunk_bytes",
            "max_prompt_bytes",
            "reply_chunk_delay_seconds",
        }
        assert (
            set(
                page.locator("button.help-button").evaluate_all(
                    "(els) => els.map(el => el.dataset.field)"
                )
            )
            == expected
        )
        for field in expected:
            button = page.locator(f'button[data-field="{field}"]')
            control = page.locator(f"#{field}")
            original = (
                control.is_checked()
                if field in {"one_shot", "radio_prompt_enabled"}
                else control.input_value()
            )
            assert button.inner_text().strip() != "?"
            assert control.get_attribute("aria-labelledby") == button.get_attribute("id")
            button.click()
            assert button.get_attribute("aria-expanded") == "true"
            assert (
                control.is_checked()
                if field in {"one_shot", "radio_prompt_enabled"}
                else control.input_value()
            ) == original
            page.keyboard.press("Escape")
            button.focus()
            page.keyboard.press("Space")
            assert button.get_attribute("aria-expanded") == "true"
            page.keyboard.press("Escape")
            page.keyboard.press("Enter")
            assert button.get_attribute("aria-expanded") == "true"
            assert page.locator("#" + button.get_attribute("aria-controls")).is_visible()
            page.keyboard.press("Escape")
            assert button.get_attribute("aria-expanded") == "false"
        button = page.locator('button[data-field="max_chunk_bytes"]')
        button.click()
        page.locator("h1").click()
        assert button.get_attribute("aria-expanded") == "false"
        assert page.locator("#one_shot").is_enabled()
        page.locator("#one_shot").uncheck()
        checkbox = page.get_by_role("checkbox", name="Radio prompt enabled", exact=True)
        original_checked = checkbox.is_checked()
        checkbox.click()
        assert checkbox.is_checked() != original_checked
        checkbox.press("Space")
        assert checkbox.is_checked() == original_checked
        assert page.get_by_role("button", name="Max chunk bytes", exact=True).count() == 1
        page.locator("#max_chunk_bytes").fill("90")
        page.locator("button[type=submit]").click()
        page.wait_for_function(
            "document.querySelector('#global-status').textContent.includes('Saved')"
        )
        assert posts[-1]["config"]["one_shot"] is False
        assert posts[-1]["restart"] is True
        assert posts[-1]["id"] == "openhop.nomad"
        assert posts[-1]["config"]["future_option"] == {"keep": True}
        assert "_runtime" not in posts[-1]["config"]
        page.locator("#reload-settings").click()
        page.wait_for_function(
            "document.querySelector('#global-status').textContent.includes('Ready')"
        )
        assert not page.locator("#one_shot").is_checked()
        assert page.locator("#max_chunk_bytes").input_value() == "90"
        assert page.locator("#max_concurrent_requests").input_value() == "1"
        for theme in ("light", "dark"):
            page.emulate_media(color_scheme=theme)
            for width in (1280, 390, 768):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(tmp_path / f"{theme}-{width}.png"), full_page=True)
                page.get_by_role("button", name="Max chunk bytes", exact=True).click()
                assert page.locator("#help-max_chunk_bytes").is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(tmp_path / f"{theme}-{width}-help.png"), full_page=True)
                page.keyboard.press("Escape")
        assert not errors
        assert not external
        browser.close()
