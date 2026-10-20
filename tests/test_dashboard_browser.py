"""Real browser interaction against the local contract-test backend."""
import threading

import pytest

from dyson.runtime import Runtime
from dyson.server import Server

playwright = pytest.importorskip("playwright.sync_api")


class BrowserBackend:
    def __init__(self):
        self.parameters = []

    def complete(self, settings, messages, parameters, tools=None):
        self.parameters.append(dict(parameters))
        return {"choices": [{"message": {"role": "assistant", "content": "browser-contract-output"},
                              "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3}}

    def health(self, settings):
        return {"data": [{"id": "browser-contract"}]}


def test_sliders_control_request_and_persist_across_browser_reload(tmp_path):
    backend = BrowserBackend()
    with Runtime(tmp_path, backend=backend) as runtime, Server(("127.0.0.1", 0), runtime) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with playwright.sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                playwright.expect(page.locator("#revision")).to_contain_text("Saved revision")
                slider = page.locator("#temperature")
                slider.focus()
                slider.press("Home")
                for _ in range(5):
                    slider.press("ArrowRight")
                page.locator("#apply").click()
                playwright.expect(page.locator("#revision")).to_contain_text("Saved revision 1")
                page.locator("#prompt").fill("test local model contract")
                page.locator("#send").click()
                playwright.expect(page.locator("#reply")).to_have_text("browser-contract-output")
                assert backend.parameters[-1]["temperature"] == 0.25
                page.locator("#progress").click()
                playwright.expect(page.locator("#sessions")).to_contain_text('"known_outcomes": 1')
                assert runtime.status()["sessions"]["dashboard"]["known_outcomes"] == 1
                page.reload()
                playwright.expect(page.locator("#temperature")).to_have_value("0.25")
                assert not errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
