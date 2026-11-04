"""`redactor fetch`: recordings, replay and the privacy promises around them.

The end-to-end tests drive the installed Chrome against a provably fake local bank
page. They skip when Playwright or Chrome is unavailable, as in CI.
"""
from __future__ import annotations

import http.server
import json
import stat
import threading
from pathlib import Path

import pytest

from redactor import fetch
from redactor.cli import main

PASSWORD = "FAKE-PASSWORD-1234"
TYPED_MEMO = "FAKE-TYPED-TEXT-5678"

PAGES = {
    "/login": f"""<!doctype html><title>Fake Test Bank</title>
<input id="user"><input id="pass" type="password" value="{PASSWORD}">
<button id="signin" onclick="location.href='/home'">Sign in</button>""",
    "/home": f"""<!doctype html><input id="memo" value="{TYPED_MEMO}">
<a href="/accounts">Accounts</a>""",
    "/accounts": """<!doctype html><iframe src="/export" width=600 height=400></iframe>""",
    "/export": """<!doctype html>
<select name="format"><option value="csv">CSV</option><option value="xls">Excel</option></select>
<button aria-label="Download history"
  onclick="location.href='/file-'+document.querySelector('select').value">Go</button>""",
}


class FakeBank(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/file-"):
            ext = self.path.removeprefix("/file-")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="ACCT-99887766.{ext}"')
            self.end_headers()
            self.wfile.write(b"date,amount\n2026-01-01,-1.00\n")
            return
        body = PAGES.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write((body or "").encode())

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def bank():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeBank)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def chrome():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            pw.chromium.launch(channel="chrome", headless=True).close()
        except Exception:
            pytest.skip("Chrome is not installed")


def _record(root: Path, url: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sign in before recording starts, then click through once recording has begun."""
    sessions: list[fetch._Session] = []
    real_session = fetch._Session

    class Spy(real_session):
        def __enter__(self):
            sessions.append(self)
            return super().__enter__()

        def wait_for_download(self, timeout_ms):
            page = self.page
            page.fill("#memo", TYPED_MEMO)
            page.click("text=Accounts")
            frame = page.frame_locator("iframe")
            frame.locator("select").select_option("xls")
            frame.locator("button").click()
            return super().wait_for_download(timeout_ms)

    monkeypatch.setattr(fetch, "_Session", Spy)

    def login() -> None:
        page = sessions[0].page
        page.fill("#user", "fake-user")
        page.click("#signin")
        page.wait_for_url("**/home")

    saved = fetch.record(root, "bank", f"{url}/login", wait_for_login=login, headless=True)
    monkeypatch.setattr(fetch, "_Session", real_session)
    return saved


def test_record_captures_only_post_login_clicks_and_replays(tmp_path, bank, chrome, monkeypatch):
    first = _record(tmp_path, bank, monkeypatch)
    recipe_file = fetch.recipe_path(tmp_path, "bank")
    raw = recipe_file.read_text()
    recipe = json.loads(raw)

    assert [s["action"] for s in recipe["steps"]] == ["click", "select", "click"]
    assert recipe["steps"][1]["value"] == "xls"
    for secret in (PASSWORD, TYPED_MEMO, "fake-user", "signin", "#user"):
        assert secret not in raw
    assert stat.S_IMODE(recipe_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert first.parent == tmp_path / "downloads"
    assert "99887766" not in first.name and first.suffix == ".xls"

    sessions: list[fetch._Session] = []

    class Spy(fetch._Session):
        def __enter__(self):
            sessions.append(self)
            return super().__enter__()

    monkeypatch.setattr(fetch, "_Session", Spy)

    def login() -> None:
        page = sessions[0].page
        page.click("#signin")
        page.wait_for_url("**/home")

    second = fetch.run(tmp_path, "bank", wait_for_login=login, headless=True)
    assert second.read_bytes() == first.read_bytes()


def test_run_stops_at_a_missing_step_without_guessing(tmp_path, bank, chrome, capsys):
    fetch.save_recipe(
        tmp_path, "bank", f"{bank}/home",
        [{"action": "click", "selector": "#SECRET-SELECTOR-account-1234"}],
    )
    with pytest.raises(fetch.FetchError, match="step 1 of 1 wasn't found"):
        fetch.run(tmp_path, "bank", wait_for_login=lambda: None, headless=True, step_timeout_ms=500)
    captured = capsys.readouterr()
    assert "SECRET-SELECTOR" not in captured.out + captured.err
    assert not (tmp_path / "downloads").exists()


def test_empty_recording_is_refused(tmp_path):
    with pytest.raises(fetch.FetchError, match="no clicks"):
        fetch.save_recipe(tmp_path, "bank", "https://example.test/", [])


@pytest.mark.parametrize("site", ["Bank", "../evil", "a/b", "", "x" * 33])
def test_bad_site_names_are_refused(site):
    with pytest.raises(fetch.FetchError):
        fetch.validate_site(site)


@pytest.mark.parametrize("url", ["http://bank.test/", "file:///etc/passwd", "javascript:alert(1)"])
def test_non_https_login_addresses_are_refused(url):
    with pytest.raises(fetch.FetchError):
        fetch.validate_url(url)


def test_status_lists_metadata_only(tmp_path, capsys):
    fetch.save_recipe(
        tmp_path, "bank", "https://example.test/login",
        [{"action": "click", "selector": "a:text-is(\"Checking 1234\")"}],
    )
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "bank-20260101-000000.xls").write_bytes(b"FAKE")

    assert main(["fetch", "--root", str(tmp_path), "status"]) == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["sites"][0]["site"] == "bank"
    assert report["downloads"][0]["name"] == "bank-20260101-000000.xls"
    assert report["downloads"][0]["bytes"] == 4
    assert "Checking 1234" not in out and "example.test" not in out


def test_cli_failure_is_value_free(tmp_path, capsys):
    assert main(["fetch", "--root", str(tmp_path), "run", "--site", "bank"]) == 2
    assert "no recording" in capsys.readouterr().err
