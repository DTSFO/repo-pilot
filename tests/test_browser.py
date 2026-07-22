from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.async_api import Browser, Page, async_playwright

from repopilot.api import create_app
from repopilot.config import Settings

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("REPOPILOT_RUN_BROWSER_TESTS") != "1",
        reason="set REPOPILOT_RUN_BROWSER_TESTS=1 after installing Chromium",
    ),
]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
async def browser_page(tmp_path: Path) -> AsyncIterator[tuple[Page, Browser, str]]:
    repository = tmp_path / "sample-repository"
    repository.mkdir()
    (repository / "README.md").write_text(
        "# Sample\n\nReport export uses safe Markdown and immutable revisions.\n",
        encoding="utf-8",
    )
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'browser.db'}",
        workspace_root=tmp_path,
        repository_root=tmp_path / "managed",
        allowed_repository_roots=str(tmp_path),
        sse_poll_seconds=0.01,
        sse_heartbeat_seconds=0.05,
    )
    app = create_app(settings)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    server.install_signal_handlers = lambda: None
    server_task = asyncio.create_task(server.serve())
    try:
        async with httpx.AsyncClient(base_url=base_url) as client:
            for _ in range(100):
                try:
                    if (await client.get("/ready")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("browser test server did not become ready")

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            yield page, browser, base_url
            await browser.close()
    finally:
        server.should_exit = True
        await server_task


async def test_multi_repository_report_render_and_downloads(
    browser_page: tuple[Page, Browser, str], tmp_path: Path
) -> None:
    page, _, base_url = browser_page
    repository = tmp_path / "sample-repository"
    console_errors: list[str] = []

    def capture_console_error(message: object) -> None:
        if getattr(message, "type", None) == "error":
            console_errors.append(str(getattr(message, "text", "")))

    page.on("console", capture_console_error)

    await page.goto(base_url)
    await page.get_by_role("button", name="添加仓库").click()
    await page.get_by_role("textbox", name="名称").fill("Browser repository")
    await page.get_by_role("textbox", name="本地绝对路径").fill(str(repository))
    await page.get_by_role("button", name="添加并索引").click()
    await page.get_by_text("Browser repository").first.wait_for()
    await page.get_by_placeholder("例如: Provider 的 SSE").fill(
        "How is report export implemented?\n\n"
        "<script>window.__repopilot_xss = true</script>\n"
        "[unsafe](javascript:alert(1))\n"
        '<img src=x onerror="window.__repopilot_xss = true">'
    )
    await page.get_by_role("button", name="创建任务").click()
    await page.get_by_text("completed").last.wait_for(timeout=15_000)

    assert await page.locator("#report h1").first.text_content() == "RepoPilot 研究报告"
    assert await page.locator("#report script,#report img,#report iframe").count() == 0
    assert await page.locator('#report a[href^="javascript:"]').count() == 0
    assert await page.locator("[onclick],[onerror],[onload]").count() == 0
    assert await page.evaluate("window.__repopilot_xss") is None

    for label, suffix in (
        ("导出 Markdown", ".md"),
        ("导出 HTML", ".html"),
        ("导出 JSON", ".json"),
    ):
        async with page.expect_download() as download_info:
            await page.get_by_role("button", name=label).click()
        download = await download_info.value
        assert download.suggested_filename.endswith(suffix)
        assert await download.failure() is None

    await page.set_viewport_size({"width": 390, "height": 844})
    dimensions = await page.evaluate(
        "() => ({scrollWidth: document.documentElement.scrollWidth, "
        "clientWidth: document.documentElement.clientWidth})"
    )
    assert dimensions["scrollWidth"] <= dimensions["clientWidth"]
    assert not console_errors
