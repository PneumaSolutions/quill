"""Sign in to Scribe for Documents (quill.core.scribe_signin).

The loopback listener is exercised for real on 127.0.0.1 with port 0 (a free
port), and the "browser" is a function that requests the redirect the way a
browser would after Scribe signs the person in. Scribe itself is the fake
module from tests/scribe_fakes.py: nothing leaves the machine.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request

import pytest

from quill.core import scribe_documents as scribe
from quill.core.scribe_signin import LoopbackCatcher, sign_in
from tests.scribe_fakes import FakeModule, memory_secrets


@pytest.fixture(autouse=True)
def _registered(monkeypatch) -> None:
    monkeypatch.setenv(scribe.SCRIBE_CLIENT_ID_ENV, "test-client")
    monkeypatch.delenv("QUILL_SAFE_MODE", raising=False)


def _browser_redirecting_to(query: str, pages: list[str]):
    """A fake browser: after 'signing in', it follows the redirect."""

    def _open(url: str) -> None:
        redirect = url.split("redirect_uri=", 1)[1]

        def _follow() -> None:
            try:
                with urllib.request.urlopen(f"{redirect}?{query}", timeout=5) as response:
                    pages.append(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                pages.append(error.read().decode("utf-8"))

        thread = threading.Thread(target=_follow, daemon=True)
        threads.append(thread)
        thread.start()

    threads: list[threading.Thread] = []
    _open.threads = threads  # type: ignore[attr-defined]
    return _open


def test_sign_in_opens_scribes_page_catches_the_code_and_stores_tokens() -> None:
    module = FakeModule()
    manager = memory_secrets()
    pages: list[str] = []
    opened: list[str] = []
    browser = _browser_redirecting_to("code=abc123", pages)

    def _open(url: str) -> None:
        opened.append(url)
        browser(url)

    tokens = sign_in(
        module=module,
        manager=manager,
        open_browser=_open,
        catcher=LoopbackCatcher(port=0),
        timeout=10,
    )
    assert tokens.access_token == "access-new"
    assert scribe.load_tokens(manager).refresh_token == "refresh-new"
    # The URL Scribe's client built is opened exactly as built.
    assert opened == [opened[0]]
    assert opened[0].startswith(scribe.SCRIBE_BASE_URL)
    exchange = next(call for call in module.calls if call[0] == "exchange")
    assert exchange[2] == "abc123"
    assert exchange[3] == "verifier-123"
    assert exchange[1].startswith("http://127.0.0.1:")
    for thread in browser.threads:
        thread.join(5)
    assert any("signed in to Scribe for Documents" in page for page in pages)
    assert '<html lang="en">' in pages[0]


def test_a_refused_sign_in_is_reported() -> None:
    module = FakeModule()
    pages: list[str] = []
    with pytest.raises(scribe.ScribeDocumentsError, match="access_denied"):
        sign_in(
            module=module,
            manager=memory_secrets(),
            open_browser=_browser_redirecting_to("error=access_denied", pages),
            catcher=LoopbackCatcher(port=0),
            timeout=10,
        )
    assert not any(call[0] == "exchange" for call in module.calls)


def test_cancel_stops_listening() -> None:
    with pytest.raises(scribe.ScribeCancelled):
        sign_in(
            module=FakeModule(),
            manager=memory_secrets(),
            open_browser=lambda _url: None,
            catcher=LoopbackCatcher(port=0),
            cancel_requested=lambda: True,
            timeout=10,
        )


def test_a_rejected_code_exchange_is_a_sentence() -> None:
    module = FakeModule()
    module.exchange_error = module.InvalidGrantError("bad verifier")
    with pytest.raises(scribe.ScribeDocumentsError, match="did not accept"):
        sign_in(
            module=module,
            manager=memory_secrets(),
            open_browser=_browser_redirecting_to("code=x", []),
            catcher=LoopbackCatcher(port=0),
            timeout=10,
        )


def test_the_placeholder_client_id_never_opens_a_browser(monkeypatch) -> None:
    # QUILL is registered now, so stand in a build that still carries the placeholder.
    monkeypatch.setenv(scribe.SCRIBE_CLIENT_ID_ENV, scribe.SCRIBE_CLIENT_ID_PLACEHOLDER)
    opened: list[str] = []
    with pytest.raises(scribe.ScribeDocumentsError, match="not registered"):
        sign_in(module=FakeModule(), manager=memory_secrets(), open_browser=opened.append)
    assert opened == []


def test_a_busy_port_is_a_sentence() -> None:
    first = LoopbackCatcher(port=0)
    first.start()
    try:
        second = LoopbackCatcher(port=first.port)
        with pytest.raises(scribe.ScribeDocumentsError, match="could not listen"):
            second.start()
    finally:
        first.close()


def test_other_paths_do_not_count() -> None:
    catcher = LoopbackCatcher(port=0)
    catcher.start()
    results: list[str] = []

    def _browser() -> None:
        base = f"http://127.0.0.1:{catcher.port}"
        try:
            urllib.request.urlopen(f"{base}/favicon.ico", timeout=5)
        except urllib.error.HTTPError as error:
            results.append(str(error.code))
        with urllib.request.urlopen(f"{base}/callback?code=ok", timeout=5) as response:
            results.append(str(response.status))

    thread = threading.Thread(target=_browser, daemon=True)
    thread.start()
    try:
        assert catcher.wait(cancel_requested=None, timeout=10) == "ok"
    finally:
        catcher.close()
    thread.join(5)
    assert results == ["404", "200"]
