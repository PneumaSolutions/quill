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
    # A scanner whose driver did not answer is kept, but as unavailable.
    assert [s.available for s in found] == [True, True, False]
    assert found[2].error == "Busy."
    assert calls[0][0][1:] == ["list"]
    assert calls[0][1] == pagescan_cli.LIST_TIMEOUT_S


def test_the_list_timeout_outlasts_the_tools_own_limit() -> None:
    # pagescan list can take about 15 s when a driver hangs (protocol.md).
    assert 20 < pagescan_cli.LIST_TIMEOUT_S <= 60


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
    monkeypatch.setattr("quill.core.paths.app_data_dir", lambda: tmp_path / "data")
    backend = pagescan_cli.PagescanBackend()
    with pytest.raises(scanning.ScannerToolMissing, match="Options, Scanning"):
        backend.list_scanners()


def test_the_tool_is_found_in_order(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pagescan_cli, "__file__", str(tmp_path / "x" / "y" / "z" / "w.py"))
    monkeypatch.setattr("quill.core.paths.app_data_dir", lambda: tmp_path / "data")
    app = tmp_path / "app" / "tools" / "scanner"
    app.mkdir(parents=True)
    (app / "pagescan.exe").write_bytes(b"MZ")
    env = {"QUILL_APP_ROOT": str(tmp_path / "app")}
    win = {"platform": "win32"}
    assert pagescan_cli.find_tool(environ=env, **win) == app / "pagescan.exe"
    custom = _tool(tmp_path)
    # The bundled copy wins over Options (independent review Q2)...
    assert pagescan_cli.find_tool(str(custom), environ=env, **win) == app / "pagescan.exe"
    # ...which stands in only when there is none.
    assert pagescan_cli.find_tool(str(custom), environ={}, **win) == custom
    assert pagescan_cli.find_tool(str(tmp_path), environ={}, **win) == custom  # a folder too
    assert pagescan_cli.find_tool(environ={}, **win) is None


def test_the_old_wia_scan_name_is_still_found_on_windows(monkeypatch, tmp_path) -> None:
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
    monkeypatch.setattr("quill.core.paths.app_data_dir", lambda: tmp_path / "data")
    tools = tmp_path / "Resources" / "tools" / "scanner"
    tools.mkdir(parents=True)
    (tools / "pagescan").write_bytes(b"\xcf\xfa\xed\xfe")
    env = {"RESOURCEPATH": str(tmp_path / "Resources")}
    assert pagescan_cli.find_tool(environ=env, platform="darwin") == tools / "pagescan"


def test_path_and_relative_paths_are_never_searched(monkeypatch, tmp_path) -> None:
    """Security review: a pagescan on PATH or in the current folder never runs."""
    monkeypatch.setattr("quill.core.paths.app_data_dir", lambda: tmp_path / "data")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "pagescan").write_bytes(b"x")
    (tmp_path / "bin" / "pagescan").chmod(0o755)
    monkeypatch.chdir(tmp_path / "bin")
    monkeypatch.setattr(pagescan_cli, "__file__", str(tmp_path / "x" / "y" / "z" / "w.py"))
    assert pagescan_cli.find_tool(environ={}, platform="darwin") is None
    assert pagescan_cli.find_tool("pagescan", environ={}, platform="darwin") is None
    env = {pagescan_cli.TOOL_ENV: "pagescan"}
    assert pagescan_cli.find_tool(environ=env, platform="darwin") is None


def test_the_bundled_tool_wins_over_the_environment_variable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("quill.core.paths.app_data_dir", lambda: tmp_path / "data")
    app = tmp_path / "app" / "tools" / "scanner"
    app.mkdir(parents=True)
    (app / "pagescan").write_bytes(b"x")
    other = tmp_path / "pagescan"
    other.write_bytes(b"x")
    env = {"QUILL_APP_ROOT": str(tmp_path / "app"), pagescan_cli.TOOL_ENV: str(other)}
    assert pagescan_cli.find_tool(environ=env, platform="darwin") == app / "pagescan"
    env.pop("QUILL_APP_ROOT")
    monkeypatch.setattr(pagescan_cli, "__file__", str(tmp_path / "x" / "y" / "z" / "w.py"))
    assert pagescan_cli.find_tool(environ=env, platform="darwin") == other


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


def test_only_pages_inside_the_scan_folder_are_taken(tmp_path) -> None:
    """Security review: the tool's answer cannot point QUILL at another file."""
    out = tmp_path / "out"
    out.mkdir()
    page = out / "scan-001.png"
    page.write_bytes(b"png")
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    reply = {"ok": True, "files": [str(secret), str(out / ".." / "secret.txt"), str(page)]}

    def spawn(_args):
        return _Process(json.dumps(reply))

    backend = pagescan_cli.PagescanBackend(_tool(tmp_path), spawn=spawn, sleep=lambda _s: None)
    assert backend.scan("dev", ScanOptions(), out) == [page]
    reply["files"] = [str(secret)]
    with pytest.raises(scanning.ScanError, match="no page"):
        backend.scan("dev", ScanOptions(), out)


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
    page = tmp_path / "o" / "p.png"
    page.parent.mkdir()
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
    page = tmp_path / "o" / "p1.png"
    page.parent.mkdir()
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


def test_options_can_never_point_at_a_network_or_another_program(monkeypatch, tmp_path) -> None:
    """Independent review Q2: a shared settings file chose which program ran."""
    monkeypatch.setattr("quill.core.paths.app_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(pagescan_cli, "__file__", str(tmp_path / "x" / "y" / "z" / "w.py"))
    win = {"platform": "win32"}
    for unc in (r"\\server\share\pagescan.exe", "//server/share/pagescan.exe"):
        assert pagescan_cli.find_tool(unc, environ={}, **win) is None
        assert not pagescan_cli.is_local_path(unc, "win32")
        assert not pagescan_cli.is_local_path(unc, "darwin")
    renamed = tmp_path / "evil.exe"
    renamed.write_bytes(b"MZ")
    assert pagescan_cli.find_tool(str(renamed), environ={}, **win) is None
    assert pagescan_cli.is_local_path(r"C:\Tools\pagescan.exe", "win32") is True
    monkeypatch.setattr(pagescan_cli, "_drive_is_remote", lambda drive: drive.upper() == "Z:")
    assert not pagescan_cli.is_local_path(r"Z:\Tools\pagescan.exe", "win32")  # mapped drive
    assert not pagescan_cli.is_local_path(r"C:Tools\pagescan.exe", "win32")  # drive-relative


def test_scanner_and_download_paths_never_travel_in_a_portable_export() -> None:
    from quill.core.settings_registry import LOCAL_SETTINGS

    assert {
        "scanner_tool_path",
        "scanner_device",
        "scanner_device_name",
        "scribe_download_folder",
    } <= LOCAL_SETTINGS
