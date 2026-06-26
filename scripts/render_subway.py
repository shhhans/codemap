"""Render web/subway.html → PNG using the pre-installed Chromium (Milestone 4 helper).

Embeds the exported subway_map.json into the page (avoids file:// fetch/CORS),
then screenshots the full diagram. Requires `playwright` (pip) and a Chromium
build — in this environment it's at $PLAYWRIGHT_BROWSERS_PATH; elsewhere set
CHROME_PATH or run `playwright install chromium`.

    python -m codemap.export --db .codemap/blackboard.sqlite --out web/subway_map.json
    python scripts/render_subway.py web/subway_map.png
"""

from __future__ import annotations

import glob
import os
import pathlib
import sys

from playwright.sync_api import sync_playwright


def _find_chrome() -> str | None:
    if os.getenv("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
    roots = [os.getenv("PLAYWRIGHT_BROWSERS_PATH", ""), os.path.expanduser("~/.cache/ms-playwright")]
    for root in roots:
        if not root:
            continue
        for exe in ("chrome", "headless_shell"):
            hits = glob.glob(os.path.join(root, "chromium*", "chrome-linux", exe))
            if hits:
                return sorted(hits)[-1]
    return None  # fall back to Playwright's own resolution


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "web/subway_map.png"
    html = pathlib.Path("web/subway.html").read_text(encoding="utf-8")
    data = pathlib.Path("web/subway_map.json").read_text(encoding="utf-8")
    html = html.replace("</head>", f"<script>window.SUBWAY_DATA = {data};</script>\n</head>", 1)

    chrome = _find_chrome()
    launch: dict = {"args": ["--no-sandbox"]}
    if chrome:
        launch["executable_path"] = chrome

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        page = browser.new_page(device_scale_factor=2)
        page.set_content(html, wait_until="load")
        page.wait_for_timeout(500)
        page.screenshot(path=out, full_page=True)
        browser.close()
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
