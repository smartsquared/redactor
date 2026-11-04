"""Download statements from a bank site with a replayed recording. No model involved.

``redactor fetch`` opens a visible Chrome window. The human logs in, then either
records the clicks that reach the export once (``record``) or lets the recording
replay (``run``). The file lands in a private staging directory.

What keeps this safe:

* **The password is never handled.** Recording starts only after the human says
  they are logged in; the page listeners do not exist before that point.
* **Typed text is never recorded.** Only clicks and ``<select>`` choices are.
* **Nothing identifying is printed.** Status lines name a site and a step number,
  never a selector, URL or page text. Recipes can contain account labels, so they
  stay on this machine under ``~/.redactor/fetch`` with owner-only permissions.
* **It stops rather than guesses.** A step that cannot be found ends the run with
  a non-zero exit. The human can always download by hand instead.

Downloads go to ``~/.redactor/downloads``, not the ingest inbox. Bank exports can
hold the account's full history and ``ingest`` is not idempotent (issue #45), so a
file must be filtered to new rows before it is ingested.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

RECIPE_VERSION = 1
SITE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
STEP_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 120_000
RECORD_TIMEOUT_MS = 15 * 60_000

# Installed into each frame only after the human confirms login. It reports clicks
# and <select> choices. It never reads the value of a text or password field.
RECORDER_JS = r"""
(() => {
  if (window.__redactorFetchRecorder) return;
  window.__redactorFetchRecorder = true;
  const quote = (s) => '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  const text = (el) => (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
  const cssPath = (el) => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
      const same = n.parentElement
        ? [...n.parentElement.children].filter((c) => c.tagName === n.tagName) : [n];
      parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + (same.indexOf(n) + 1) + ')');
    }
    return parts.join(' > ');
  };
  const selector = (el) => {
    const tag = el.tagName.toLowerCase();
    if (el.id && !/\d{3,}/.test(el.id)) return '#' + CSS.escape(el.id);
    const testId = el.getAttribute('data-testid');
    if (testId) return '[data-testid=' + quote(testId) + ']';
    const name = el.getAttribute('name');
    if (name) return tag + '[name=' + quote(name) + ']';
    const label = el.getAttribute('aria-label');
    if (label) return tag + '[aria-label=' + quote(label) + ']';
    if (tag === 'input' && el.value && ['submit', 'button'].includes(el.type)) {
      return 'input[value=' + quote(el.value) + ']';
    }
    const t = text(el);
    if (t && t.length <= 60 && ['a', 'button', 'span', 'li', 'td'].includes(tag)) {
      return tag + ':text-is(' + quote(t) + ')';
    }
    return cssPath(el);
  };
  const SKIP = new Set(['text', 'password', 'email', 'tel', 'number', 'search', 'url', 'date']);
  document.addEventListener('click', (e) => {
    if (!(e.target instanceof Element)) return;
    const el = e.target.closest('a,button,input,label,select,option,[role=button],[onclick]')
      || e.target;
    const tag = el.tagName.toLowerCase();
    if (tag === 'select' || tag === 'option' || tag === 'textarea') return;
    if (tag === 'input' && SKIP.has(el.type)) return;
    window.__redactorFetchStep({ action: 'click', selector: selector(el) });
  }, true);
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (el instanceof HTMLSelectElement) {
      window.__redactorFetchStep({ action: 'select', selector: selector(el), value: el.value });
    }
  }, true);
})();
"""


class FetchError(Exception):
    """A value-free failure, safe to print and copy-paste."""


def default_root() -> Path:
    return Path.home() / ".redactor"


def validate_site(site: str) -> str:
    if not SITE_RE.fullmatch(site):
        raise FetchError("site must be lowercase letters, digits or dashes, like 'bank'")
    return site


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    local = parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS
    if not local and not (parsed.scheme == "https" and parsed.hostname):
        raise FetchError("the login address must start with https://")
    return url


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def recipe_path(root: Path, site: str) -> Path:
    return root / "fetch" / f"{validate_site(site)}.json"


def save_recipe(root: Path, site: str, url: str, steps: list[dict[str, str]]) -> Path:
    if not steps:
        raise FetchError("no clicks were recorded, so the recording was not saved")
    path = recipe_path(root, site)
    _private_dir(path.parent)
    recipe = {"version": RECIPE_VERSION, "site": site, "url": url, "steps": steps}
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(recipe, fh, indent=2)
    tmp.replace(path)
    return path


def load_recipe(root: Path, site: str) -> dict[str, Any]:
    path = recipe_path(root, site)
    if not path.is_file():
        raise FetchError(f"no recording for '{site}' yet; run record first")
    try:
        recipe = json.loads(path.read_text(encoding="utf-8"))
        steps = recipe["steps"]
        recipe["url"]
    except (ValueError, KeyError, TypeError) as exc:
        raise FetchError(f"the recording for '{site}' is unreadable; record it again") from exc
    if recipe.get("version") != RECIPE_VERSION or not isinstance(steps, list) or not steps:
        raise FetchError(f"the recording for '{site}' is unreadable; record it again")
    return recipe


def status(root: Path) -> dict[str, list[dict[str, Any]]]:
    """Metadata only: recorded sites and downloaded file names, sizes and times."""
    fetch_dir = root / "fetch"
    sites = []
    if fetch_dir.is_dir():
        for path in sorted(fetch_dir.glob("*.json")):
            if SITE_RE.fullmatch(path.stem):
                sites.append({"site": path.stem, "recorded_at": _mtime(path)})
    downloads = []
    download_dir = root / "downloads"
    if download_dir.is_dir():
        files = [p for p in download_dir.iterdir() if p.is_file() and p.name[0] != "."]
        for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
            downloads.append(
                {"name": path.name, "bytes": path.stat().st_size, "saved_at": _mtime(path)}
            )
    return {"sites": sites, "downloads": downloads}


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds")


def _say(message: str) -> None:
    print(message, flush=True)


def _wait_for_enter() -> None:
    if not sys.stdin.readline():
        raise FetchError("stopped before login was confirmed")


class _Session:
    """A visible Chrome window with its own private profile for one site."""

    def __init__(self, root: Path, site: str, *, headless: bool = False) -> None:
        self.root = root
        self.site = site
        self.headless = headless
        self.downloads: list[Any] = []

    def __enter__(self) -> _Session:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise FetchError(
                "the download tool isn't installed; run: pip install 'redactor[fetch]'"
            ) from exc
        self._pw = sync_playwright().start()
        profile = _private_dir(self.root / "fetch" / "profiles" / self.site)
        try:
            self.context = self._pw.chromium.launch_persistent_context(
                str(profile), channel="chrome", headless=self.headless, accept_downloads=True
            )
        except Exception as exc:
            self._pw.stop()
            raise FetchError("Chrome could not be opened") from exc
        self.context.on("page", self._watch)
        for page in self.context.pages:
            self._watch(page)
        return self

    def __exit__(self, *exc_info: object) -> None:
        try:
            self.context.close()
        except Exception:  # noqa: BLE001 - the human may have closed the window already
            pass
        self._pw.stop()

    def _watch(self, page: Any) -> None:
        page.on("download", lambda download: self.downloads.append(download))

    @property
    def page(self) -> Any:
        return self.context.pages[-1] if self.context.pages else self.context.new_page()

    def pump(self, ms: int = 200) -> None:
        """Let Playwright deliver events, using whichever page is still open."""
        self.page.wait_for_timeout(ms)

    def wait_for_download(self, timeout_ms: int) -> Any:
        waited = 0
        while not self.downloads:
            if waited >= timeout_ms:
                return None
            self.pump()
            waited += 200
        return self.downloads[0]

    def save_download(self, download: Any) -> Path:
        out_dir = _private_dir(self.root / "downloads")
        # The bank's own file name can carry an account number, so keep only the type.
        ext = Path(download.suggested_filename).suffix.lower()
        ext = ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else ".dat"
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"{self.site}-{stamp}{ext}"
        if path.exists():
            raise FetchError("a download with this name already exists; try again in a moment")
        download.save_as(str(path))
        path.chmod(0o600)
        return path


def record(
    root: Path,
    site: str,
    url: str,
    *,
    wait_for_login: Callable[[], None] = _wait_for_enter,
    headless: bool = False,
    timeout_ms: int = RECORD_TIMEOUT_MS,
) -> Path:
    """Record the clicks from a logged-in page through to the first download."""
    validate_site(site)
    validate_url(url)
    steps: list[dict[str, str]] = []
    with _Session(root, site, headless=headless) as session:
        session.page.goto(url)
        _say("Log in in the Chrome window. Nothing is being recorded yet.")
        _say("WAITING_FOR_LOGIN")
        wait_for_login()

        def on_step(_source: Any, step: dict[str, str]) -> None:
            if not session.downloads:
                steps.append(
                    {k: str(step[k]) for k in ("action", "selector", "value") if k in step}
                )

        session.context.expose_binding("__redactorFetchStep", on_step)
        session.context.add_init_script(RECORDER_JS)
        for page in session.context.pages:
            for frame in page.frames:
                frame.evaluate(RECORDER_JS)
        _say("Recording. Click through to the export and download the file.")
        download = session.wait_for_download(timeout_ms)
        if download is None:
            raise FetchError("no download happened, so the recording was not saved")
        save_recipe(root, site, url, steps)
        saved = session.save_download(download)
    _say(f"Saved a recording of {len(steps)} steps for {site}.")
    _say(f"Downloaded {saved.name}.")
    return saved


def _locate(session: _Session, selector: str, timeout_ms: int) -> Any:
    waited = 0
    while waited < timeout_ms:
        for page in session.context.pages:
            for frame in page.frames:
                try:
                    locator = frame.locator(selector).first
                    if locator.count() and locator.is_visible():
                        return locator
                except Exception:  # noqa: BLE001 - a frame can vanish mid-navigation
                    continue
        session.pump(250)
        waited += 250
    return None


def run(
    root: Path,
    site: str,
    *,
    wait_for_login: Callable[[], None] = _wait_for_enter,
    headless: bool = False,
    step_timeout_ms: int = STEP_TIMEOUT_MS,
    download_timeout_ms: int = DOWNLOAD_TIMEOUT_MS,
) -> Path:
    """Replay a recording after the human logs in, and save the downloaded file."""
    recipe = load_recipe(root, validate_site(site))
    steps = recipe["steps"]
    with _Session(root, site, headless=headless) as session:
        session.page.goto(validate_url(recipe["url"]))
        _say("Log in in the Chrome window. The script is waiting.")
        _say("WAITING_FOR_LOGIN")
        wait_for_login()
        for number, step in enumerate(steps, start=1):
            _say(f"Step {number} of {len(steps)}.")
            locator = _locate(session, step["selector"], step_timeout_ms)
            if locator is None:
                raise FetchError(
                    f"step {number} of {len(steps)} wasn't found. The site may have changed. "
                    "Nothing was guessed; download by hand or record again."
                )
            try:
                if step["action"] == "select":
                    locator.select_option(step["value"], timeout=step_timeout_ms)
                else:
                    locator.click(timeout=step_timeout_ms)
            except Exception:  # noqa: BLE001 - Playwright messages can echo page text
                raise FetchError(
                    f"step {number} of {len(steps)} didn't work. Nothing was guessed; "
                    "download by hand or record again."
                ) from None
        download = session.wait_for_download(download_timeout_ms)
        if download is None:
            raise FetchError("all steps ran but no file downloaded; record again")
        saved = session.save_download(download)
    _say(f"Downloaded {saved.name}.")
    return saved


def command(args: Any) -> int:
    """Entry point for ``redactor fetch``; failures print value-free messages."""
    root = Path(args.root).expanduser() if args.root else default_root()
    try:
        if args.fetch_command == "status":
            print(json.dumps(status(root), indent=2))
        elif args.fetch_command == "record":
            record(root, args.site, args.url)
        else:
            run(root, args.site)
    except FetchError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - never echo a browser error, it can hold page text
        print(f"Stopped: the browser step failed ({type(exc).__name__}).", file=sys.stderr)
        return 3
    return 0
