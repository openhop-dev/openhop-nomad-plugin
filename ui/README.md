# NOMAD configuration UI

This dependency-free plugin page uses openHop RepeaterUI's blue/purple palette and cool slate light/dark surfaces (`src/assets/base.css` in openhop-dev/openHop_RepeaterUI). It follows the browser/embedding page color scheme, uses system fonts, and loads no CDN resources.

The official Project N.O.M.A.D. artwork is bundled under `assets/`, including its pinned provenance and upstream license. Keep these files in both source and wheel distributions. The runtime image path is relative so `/plugins/openhop.nomad/` works offline.

Per-option click help covers Connectivity, Bridge behavior and Radio payload limits. Click the setting name (subtly underlined), not a separate question-mark icon. Checkbox names open help without toggling; click the checkbox itself to change its value. The bundled logo replaces the graph icon beside the NOMAD Bridge header, not in the overview card. Native name buttons support Enter/Space, expose `aria-expanded` and `aria-controls`, and dismiss on Escape, another help button, or an outside click. Help is inline rather than a clipping-prone overlay. One-shot defaults on and is togglable. Off enables bounded per-sender RAM memory (10 pairs / 16 KiB, 64 senders, 30-minute expiry); restart clears it. Save/reload preserves false as well as true.

The current settings API and restart request remain unchanged. Unknown configuration keys are retained; `_runtime` is excluded. Displayed defaults match `config.default.json`; the newer admission/rate/pacing controls are included instead of silently dropping them on save.

## Local browser regression

In an isolated virtual environment:

```sh
pip install -e '.[dev]' playwright
playwright install chromium
pytest tests/test_webui.py
```

The browser test intercepts every request at a fictional origin, serves actual bundled files, and uses an in-memory settings API. It exercises help, saving/reloading, unknown-key preservation, offline image loading, and light/dark layouts at desktop, tablet and mobile widths. It does not connect to a live Repeater, NOMAD service, or radio. Playwright is optional for the ordinary Python suite; install it explicitly to exercise this browser test rather than skip it.
