"""Contact role and encoded path labels match the Companion protocol."""

import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_contact_roles_paths_and_filter():
    sync = pytest.importorskip("playwright.sync_api")
    contacts = [
        {"public_key": f"{i:02x}" * 32, "name": name, "adv_type": i,
         "out_path_len": path, "last_advert": 0}
        for i, name, path in [
            (0, "Unclassified", -1), (1, "Yellowcooln", 0),
            (2, "YC-Base-Repeater", 0x80), (3, "Room", 0x42),
            (4, "Sensor", 0x81),
        ]
    ]
    result = None
    favorites = set()
    errors = []
    with sync.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(request):
            nonlocal result
            url = request.request.url
            assert url.startswith("http://nomad.test/")
            if "/api/plugins/runtime" in url:
                return request.fulfill(json={"runtime": {"contacts": {
                    "token": "a" * 64, "connected": True, "updated_at": time.time(),
                    "endpoint": "running:5050", "favorites": sorted(favorites), "result": result,
                }}})
            if "/api/plugins/settings" in url:
                if request.request.method == "POST":
                    action = request.request.post_data_json["config"].get("contacts_request")
                    assert action and action["action"] in ("list", "favorite", "unfavorite")
                    if action["action"] == "favorite":
                        favorites.add(action["public_key"])
                    elif action["action"] == "unfavorite":
                        favorites.remove(action["public_key"])
                    result = {"id": action["id"], "action": action["action"], "status": "ok"}
                    if action["action"] == "list":
                        result.update(contacts=contacts, total=len(contacts))
                return request.fulfill(json={"config": json.loads((ROOT / "config.default.json").read_text())})
            assert "/api/" not in url
            filename = url.split("/plugins/openhop.nomad/")[-1] or "index.html"
            return request.fulfill(path=str(ROOT / "ui" / filename))

        page.route("**/*", route)
        page.goto("http://nomad.test/plugins/openhop.nomad/")
        page.get_by_role("tab", name="Contacts").click()
        page.locator("#contacts-refresh").click()
        page.wait_for_function("document.querySelectorAll('.contact-row').length === 5")
        rows = {row.locator(".contact-name").text_content(): row.locator(".contact-meta").text_content()
                for row in page.locator(".contact-row").all()}
        assert "Unknown" in rows["Unclassified"] and "unknown path" in rows["Unclassified"]
        assert "Companion" in rows["Yellowcooln"] and "direct" in rows["Yellowcooln"]
        assert "Repeater" in rows["YC-Base-Repeater"] and "direct" in rows["YC-Base-Repeater"]
        assert "Room Server" in rows["Room"] and "2 hops" in rows["Room"]
        assert "Sensor" in rows["Sensor"] and "1 hop" in rows["Sensor"]
        assert [(o.get_attribute("value"), o.inner_text()) for o in
                page.locator("#contacts-type-filter option").all()] == [
                    ("all", "All types"), ("0", "Unknown"), ("1", "Companion"),
                    ("2", "Repeater"), ("3", "Room Server"), ("4", "Sensor")]
        page.locator("#contacts-type-filter").select_option("2")
        assert page.locator(".contact-row .contact-name").all_inner_texts() == ["YC-Base-Repeater"]
        page.locator(".contact-row .fav-btn").click()
        page.wait_for_function("document.querySelector('.contact-row .fav-btn').textContent === '★'")
        assert "Added to favorites" in page.locator("#contacts-status").inner_text()
        page.locator("#contacts-filter").select_option("favorites")
        assert page.locator(".contact-row .contact-name").all_inner_texts() == ["YC-Base-Repeater"]
        page.locator(".contact-row .fav-btn").click()
        page.wait_for_function("document.querySelectorAll('.contact-row').length === 0")
        assert "Removed from favorites" in page.locator("#contacts-status").inner_text()
        assert not favorites
        assert not errors
        browser.close()
