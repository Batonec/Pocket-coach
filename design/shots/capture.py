"""Снимает экраны из макета в WebP для README.

Единственный скрипт в репозитории с pip-зависимостями вне coach_mcp — он не
участвует ни в сборке, ни в CI, ни в деплое, и запускается руками после
изменения макетов:

    pip install playwright pillow && playwright install chromium
    python3 design/shots/capture.py

Что делает: поднимает статику из `design/`, рендерит `shots/index.html`
безголовым Chromium в 2x, снимает каждый элемент `#shot-<id>` с прозрачным
фоном и кладёт уменьшенный WebP в `docs/assets/`.
"""

import functools
import http.server
import pathlib
import socketserver
import threading

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]
DESIGN = ROOT / "design"
OUT = ROOT / "docs" / "assets"
PORT = 8765

# Ширина итоговой картинки: телефоны показываются в README колонкой ~330px,
# 820px хватает на retina с запасом.
WIDTHS = {"hero": 1760, "voice": 1440}
DEFAULT_WIDTH = 820
QUALITY = 90

IDS = [
    "hero",
    "voice",
    "coach",
    "coach-why",
    "today",
    "today-start",
    "quickadd",
    "picker",
    "history",
    "events",
    "progress",
    "exercise",
    "weight",
]


def serve():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DESIGN))
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def shrink(path: pathlib.Path) -> None:
    image = Image.open(path).convert("RGBA")
    target = WIDTHS.get(path.stem, DEFAULT_WIDTH)
    if image.width > target:
        image = image.resize((target, round(image.height * target / image.width)), Image.LANCZOS)
    webp = path.with_suffix(".webp")
    image.save(webp, "WEBP", quality=QUALITY, method=6)
    path.unlink()
    print(f"{webp.name:18} {image.width}x{image.height}  {webp.stat().st_size // 1024} KB")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    httpd = serve()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1600, "height": 1200}, device_scale_factor=2)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{PORT}/shots/index.html")
            page.wait_for_selector("#shot-weight", timeout=30_000)
            page.wait_for_timeout(2_500)  # шрифты Google Fonts + раскладка графиков
            if errors:
                raise SystemExit(f"ошибки JS в макете: {errors[:3]}")
            for shot_id in IDS:
                raw = OUT / f"{shot_id}.png"
                page.locator(f"#shot-{shot_id}").screenshot(path=str(raw), omit_background=True)
                shrink(raw)
            browser.close()
    finally:
        httpd.shutdown()


main()
