"""The pagescan backend (Windows and macOS), driven with a fake runner and process.

Pins the JSON contract (list / scan, pagescan's docs/protocol.md), the source
chosen by kind and never by id, where the tool is found (with the old
wia-scan.exe name still found on Windows), cancellation through the tool's
stdin, and the sentences a listener hears when the tool is missing or the
scanner refuses.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quill.core import scanning
from quill.core.scanning import pagescan_cli
from quill.core.scanning.base import ScanOptions


def _tool(tmp_path):
    exe = tmp_path / "pagescan.exe"
    exe.write_bytes(b"MZ")
    return exe


def test_listing_reads_the_json_contract(tmp_path) -> None:
    calls = []
    reply = {
        "ok": True,
        "scanners": [
            {"id": "{6BDD}\\0000", "name": "Canon LiDE 300", "sources": [{"kind": "flatbed"}]},
            {"id": "{6BDD}\\0001", "name": "Sheet feeder", "sources": [{"kind": "feeder"}]},
            {"id": "x", "name": "Unread", "sources": [], "error": "Busy."},
            {"name": "no id, skipped"},
        ],
    }

    def runner(args, timeout):
        calls.append((args, timeout))
        return 0, "noise\n" + json.dumps(reply) + "\n"

    backend = pagescan_cli.PagescanBackend(_tool(tmp_path), runner=runner)
    found = backend.list_scanners()
    assert [s.name for s in found] == ["Canon LiDE 300", "Sheet feeder", "Unread"]
    assert found[1].has_feeder and not found[0].has_feeder
    assert [s.preferred_source for s in found] == ["flatbed", "feeder", "flatbed"]
    assert calls[0][0][1:] == ["list"]
    assert calls[0][1] == pagescan_cli.LIST_TIMEOUT_S


def test_a_failed_list_passes_on_the_tools_sentence(tmp_path) -> None:
    backend = pagescan_cli.PagescanBackend(
        _tool(tmp_path),
        runner=lambda a, t: (1, json.dumps({"ok": False, "error": "WIA is not running."})),
    )
    with pytest.raises(scanning.ScanError, match="WIA is not running"):
        backend.list_scanners()


def test_a_list_that_hangs_is_a_sentence(tmp_path) -> None:
    def runner(args, timeout):
        raise subprocess.TimeoutExpired(args, timeout)

    with pytest.raises(scanning.ScanError, match="took too long"):
        pagescan_cli.PagescanBackend(_tool(tmp_path), runner=runner).list_scanners()


def test_garbage_output_is_a_sentence() -> None:
    with pytest.raises(scanning.ScanError, match="could not read"):
        pagescan_cli.parse_reply("not json at all")


def test_a_missing_tool_says_how_to_fix_it(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(pagescan_cli.TOOL_ENV, raising=False)
    monkeypatch.delenv("QUILL_APP_ROOT", raising=False)
    monkeypatch.setattr(pagescan_cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr("quill.core.paths.app_data_dir", lambda: tmp_path / "data")
    backend = pagescan_cli.PagescanBackend()
    with pytest.raises(scanning.ScannerToolMissing, match="Options, Scanning"):
        backend.list_scanners()


def test_the_tool_is_found_in_order(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pagescan_cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr("quill.core.paths.app_data_dir", lambda: tmp_path / "data")
    app = tmp_path / "app" / "tools" / "scanner"
    app.mkdir(parents=True)
    (app / "pagescan.exe").write_bytes(b"MZ")
    env = {"QUILL_APP_ROOT": str(tmp_path / "app")}
    win = {"platform": "win32"}
    assert pagescan_cli.find_tool(environ=env, **win) == app / "pagescan.exe"
    custom = _tool(tmp_path)
    assert pagescan_cli.find_tool(str(custom), environ=env, **win) == custom
    assert pagescan_cli.find_tool(str(tmp_path), environ=env, **win) == custom  # a folder too
    assert pagescan_cli.find_tool(environ={}, **win) is None


def test_the_old_wia_scan_name_is_still_found_on_windows(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pagescan_cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr("quill.core.paths.app_data_dir", lambda: tmp_path / "data")
    app = tmp_path / "app" / "tools" / "scanner"
    app.mkdir(parents=True)
    (app / "wia-scan.exe").write_bytes(b"MZ")
    env = {"QUILL_APP_ROOT": str(tmp_path / "app")}
    assert pagescan_cli.find_tool(environ=env, platform="win32") == app / "wia-scan.exe"
    (app / "pagescan.exe").write_bytes(b"MZ")  # the new name wins once it is there
    assert pagescan_cli.find_tool(environ=env, platform="win32") == app / "pagescan.exe"
    # A Mac never looks for the Windows tool.
    assert pagescan_cli.find_tool(environ=env, platform="darwin") is None


def test_a_mac_app_finds_the_tool_in_its_resources(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pagescan_cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr("quill.core.paths.app_data_dir", lambda: tmp_path / "data")
    tools = tmp_path / "Resources" / "tools" / "scanner"
    tools.mkdir(parents=True)
    (tools / "pagescan").write_bytes(b"\xcf\xfa\xed\xfe")
    env = {"RESOURCEPATH": str(tmp_path / "Resources")}
    assert pagescan_cli.find_tool(environ=env, platform="darwin") == tools / "pagescan"


def test_path_is_the_last_place_looked(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("quill.core.paths.app_data_dir", lambda: tmp_path / "data")
    looked = []

    def which(name):
        looked.append(name)
        return "/usr/local/bin/pagescan" if name == "pagescan" else None

    monkeypatch.setattr(pagescan_cli.shutil, "which", which)
    assert pagescan_cli.find_tool(environ={}, platform="darwin") == Path("/usr/local/bin/pagescan")
    looked.clear()
    pagescan_cli.find_tool(environ={}, platform="win32")
    assert looked == ["pagescan.exe", "wia-scan.exe"]


class _Process:
    def __init__(self, stdout: str, polls: int = 2) -> None:
        self._stdout = stdout
        self._polls = polls
        self.written = b""
        self.killed = False
        self.stdin = self

    def poll(self):
        self._polls -= 1
        return None if self._polls > 0 else 0

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def communicate(self):
        return self._stdout.encode(), b""

    def kill(self):
        self.killed = True

    def wait(self):
        return 0


def test_a_scan_sends_the_contract_arguments_and_returns_pages(tmp_path) -> None:
    page = tmp_path / "out" / "scan-001.png"
    page.parent.mkdir()
    page.write_bytes(b"png")
    spawned = []

    def spawn(args):
        spawned.append(args)
        return _Process(json.dumps({"ok": True, "files": [str(page)], "error": None}))

    backend = pagescan_cli.PagescanBackend(_tool(tmp_path), spawn=spawn, sleep=lambda _s: None)
    pages = backend.scan("dev", ScanOptions(300, "gray"), tmp_path / "out")
    assert pages == [page]
    assert spawned[0][1:] == [
        "scan", "--device", "dev", "--source", "flatbed", "--dpi", "300", "--gray",
        "--out", str(tmp_path / "out"), "--format", "png",
        "--timeout", str(pagescan_cli.TOOL_TIMEOUT_S),
    ]  # fmt: skip
    assert pagescan_cli.TOOL_TIMEOUT_S < pagescan_cli.SCAN_TIMEOUT_S
    color = backend.scan_args("dev", ScanOptions(600, "color"), tmp_path)
    assert "--color" in color and "600" in color


def test_the_source_is_always_named_by_kind(tmp_path) -> None:
    backend = pagescan_cli.PagescanBackend(_tool(tmp_path))

    def source_of(args):
        return args[args.index("--source") + 1]

    assert source_of(backend.scan_args("d", ScanOptions(), tmp_path, "feeder")) == "feeder"
    assert source_of(backend.scan_args("d", ScanOptions(), tmp_path, "flatbed")) == "flatbed"
    # A source id (they change between runs on Windows) or "auto" is never sent.
    for odd in ("SWD\\Escl\\x\\Feeder", "auto", ""):
        assert source_of(backend.scan_args("d", ScanOptions(), tmp_path, odd)) == "flatbed"


def test_stderr_is_never_read_as_the_answer(tmp_path) -> None:
    page = tmp_path / "p.png"
    page.write_bytes(b"png")

    class Noisy(_Process):
        def communicate(self):
            return (
                json.dumps({"ok": True, "files": [str(page)]}).encode(),
                b'pagescan: WIA 1.0\n{"ok": false, "error": "not me"}\n',
            )

    backend = pagescan_cli.PagescanBackend(
        _tool(tmp_path), spawn=lambda a: Noisy(""), sleep=lambda _s: None
    )
    assert backend.scan("dev", ScanOptions(), tmp_path / "o") == [page]


def test_a_paper_jam_keeps_the_pages_that_finished(tmp_path) -> None:
    page = tmp_path / "p1.png"
    page.write_bytes(b"png")
    reply = {"ok": False, "files": [str(page)], "error": "Paper jam."}
    backend = pagescan_cli.PagescanBackend(
        _tool(tmp_path), spawn=lambda a: _Process(json.dumps(reply)), sleep=lambda _s: None
    )
    assert backend.scan("dev", ScanOptions(), tmp_path / "o") == [page]


def test_a_scan_error_is_the_tools_sentence(tmp_path) -> None:
    reply = {"ok": False, "files": [], "error": "The scanner is offline."}
    backend = pagescan_cli.PagescanBackend(
        _tool(tmp_path), spawn=lambda a: _Process(json.dumps(reply)), sleep=lambda _s: None
    )
    with pytest.raises(scanning.ScanError, match="offline"):
        backend.scan("dev", ScanOptions(), tmp_path / "o")


def test_cancel_writes_cancel_to_the_tool(tmp_path) -> None:
    process = _Process(json.dumps({"ok": False, "files": [], "error": "Cancelled."}), polls=3)
    backend = pagescan_cli.PagescanBackend(
        _tool(tmp_path), spawn=lambda a: process, sleep=lambda _s: None
    )
    with pytest.raises(scanning.ScanCancelled):
        backend.scan("dev", ScanOptions(), tmp_path / "o", cancel_requested=lambda: True)
    assert process.written == b"cancel\n"


def test_a_scan_that_never_ends_is_stopped(tmp_path) -> None:
    process = _Process("", polls=10**9)
    now = [0.0]

    def clock():
        now[0] += 100.0
        return now[0]

    backend = pagescan_cli.PagescanBackend(
        _tool(tmp_path), spawn=lambda a: process, sleep=lambda _s: None, clock=clock
    )
    with pytest.raises(scanning.ScanError, match="took too long"):
        backend.scan("dev", ScanOptions(), tmp_path / "o")
    assert process.killed
