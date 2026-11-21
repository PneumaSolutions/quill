"""Windows scanning through the open-source ``wia-scan`` command-line tool.

``wia-scan.exe`` (Pneuma's open-scanner project, a single Rust executable
over Windows Image Acquisition 2.0) does the COM work in its own process, so
a driver that hangs or crashes takes the tool down, never QUILL. The contract,
one JSON object on stdout per command:

* ``wia-scan list`` ->
  ``{"ok": true, "scanners": [{"id": ..., "name": ..., "sources": [...]}]}``
* ``wia-scan scan --device ID --dpi 300 --gray|--color --out DIR --format png``
  -> ``{"ok": true, "files": [...], "error": null}``; writing ``cancel`` to
  its stdin stops a running scan.

On failure ``ok`` is false and ``error`` is a sentence written to be read
aloud, which QUILL passes on. The tool runs with no console window
(``CREATE_NO_WINDOW``, through :mod:`quill.stability.safe_subprocess`), with
a timeout, and only from a worker thread.

Where the tool is found, first match wins: the path in Options (Scanning
tab, ``scanner_tool_path``), the ``QUILL_SCANNER_TOOL`` environment variable,
``{app}/tools/scanner/wia-scan.exe`` beside an installed QUILL
(``QUILL_APP_ROOT``), ``<QUILL data>/tools/scanner/wia-scan.exe``, then
``PATH``. It is local-only: nothing here touches the network.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quill.core.scanning.base import (
    COLOR_COLOR,
    CancelFn,
    ScanCancelled,
    ScanError,
    ScannerBackend,
    ScannerInfo,
    ScannerToolMissing,
    ScanOptions,
)

logger = logging.getLogger(__name__)

TOOL_NAME = "wia-scan.exe"
TOOL_ENV = "QUILL_SCANNER_TOOL"
LIST_TIMEOUT_S = 60.0
SCAN_TIMEOUT_S = 900.0
_POLL_S = 0.2

MISSING_SENTENCE = (
    "QUILL's scanner tool (wia-scan.exe) is not installed. Reinstall QUILL, or "
    "set its location in Options, Scanning."
)


def find_tool(configured: str = "", *, environ: dict[str, str] | None = None) -> Path | None:
    """The first ``wia-scan.exe`` that exists, or ``None``."""
    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    for value in (configured, env.get(TOOL_ENV, "")):
        if value.strip():
            candidates.append(Path(value.strip().strip('"')))
    app_root = env.get("QUILL_APP_ROOT", "").strip()
    if app_root:
        candidates.append(Path(app_root) / "tools" / "scanner" / TOOL_NAME)
    try:
        from quill.core.paths import app_data_dir

        candidates.append(app_data_dir() / "tools" / "scanner" / TOOL_NAME)
    except Exception:  # noqa: BLE001 - no data folder: the other places still count
        pass
    for candidate in candidates:
        if candidate.is_dir():
            candidate = candidate / TOOL_NAME
        if candidate.is_file():
            return candidate
    found = shutil.which("wia-scan")
    return Path(found) if found else None


Runner = Callable[[list[str], float], tuple[int, str]]


def _run(args: list[str], timeout: float) -> tuple[int, str]:
    from quill.stability.safe_subprocess import run_subprocess_safely

    result = run_subprocess_safely(args, timeout_seconds=timeout)
    return result.returncode, result.stdout or ""


def parse_reply(stdout: str) -> dict[str, Any]:
    """The tool's JSON answer: the last line that parses as an object."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ScanError("The scanner tool gave an answer QUILL could not read. Try again.")


def _error_of(reply: dict[str, Any], fallback: str) -> str:
    message = str(reply.get("error") or "").strip()
    return message or fallback


class WiaCliBackend(ScannerBackend):
    """:class:`ScannerBackend` over ``wia-scan.exe``."""

    name = "the scanner tool"

    def __init__(
        self,
        tool: Path | None = None,
        *,
        configured: str = "",
        runner: Runner | None = None,
        spawn: Callable[[list[str]], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._tool = tool
        self._configured = configured
        self._runner = runner or _run
        self._spawn = spawn
        self._clock = clock
        self._sleep = sleep

    def tool(self) -> Path:
        tool = self._tool or find_tool(self._configured)
        if tool is None:
            raise ScannerToolMissing(MISSING_SENTENCE)
        return tool

    def list_scanners(self) -> list[ScannerInfo]:
        args = [str(self.tool()), "list"]
        try:
            _code, stdout = self._runner(args, LIST_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise ScanError(
                "Looking for scanners took too long. Check the scanner is on, then try again."
            ) from None
        except OSError as exc:
            raise ScannerToolMissing(f"{MISSING_SENTENCE} ({exc})") from None
        reply = parse_reply(stdout)
        if not reply.get("ok"):
            raise ScanError(_error_of(reply, "The scanner tool could not list scanners."))
        scanners: list[ScannerInfo] = []
        for item in reply.get("scanners") or ():
            if not isinstance(item, dict) or not item.get("id"):
                continue
            sources = item.get("sources") or ()
            feeder = any(
                isinstance(s, dict) and str(s.get("kind", "")).lower() == "feeder" for s in sources
            )
            scanners.append(
                ScannerInfo(str(item["id"]), str(item.get("name") or "Scanner"), has_feeder=feeder)
            )
        return scanners

    def scan_args(self, device_id: str, options: ScanOptions, out_dir: Path) -> list[str]:
        return [
            str(self.tool()),
            "scan",
            "--device",
            device_id,
            "--dpi",
            str(options.dpi),
            "--color" if options.color == COLOR_COLOR else "--gray",
            "--out",
            str(out_dir),
            "--format",
            "png",
        ]

    def scan(
        self,
        device_id: str,
        options: ScanOptions,
        out_dir: Path,
        *,
        cancel_requested: CancelFn | None = None,
    ) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        args = self.scan_args(device_id, options, out_dir)
        stdout = self._scan_process(args, cancel_requested)
        reply = parse_reply(stdout)
        files = [Path(str(f)) for f in reply.get("files") or () if str(f).strip()]
        files = [f for f in files if f.is_file()]
        if not reply.get("ok") and not files:
            raise ScanError(_error_of(reply, "The scan did not finish."))
        if not files:
            raise ScanError("The scanner finished but sent no page. Try again.")
        return files

    def _scan_process(self, args: list[str], cancel_requested: CancelFn | None) -> str:
        """Run a scan, polling so Cancel can stop it; return its stdout."""
        if self._spawn is not None:
            process = self._spawn(args)
        else:
            from quill.stability.safe_subprocess import spawn_subprocess_safely

            try:
                process = spawn_subprocess_safely(args)
            except OSError as exc:
                raise ScannerToolMissing(f"{MISSING_SENTENCE} ({exc})") from None
        deadline = self._clock() + SCAN_TIMEOUT_S
        cancelled = False
        while process.poll() is None:
            if not cancelled and cancel_requested is not None and cancel_requested():
                cancelled = True
                _write_cancel(process)
                deadline = min(deadline, self._clock() + 15.0)
            if self._clock() >= deadline:
                process.kill()
                process.wait()
                if cancelled:
                    raise ScanCancelled("Scan stopped.")
                raise ScanError("The scan took too long and was stopped. Try again.")
            self._sleep(_POLL_S)
        out, _err = process.communicate()
        if cancelled:
            raise ScanCancelled("Scan stopped.")
        if isinstance(out, bytes):
            return out.decode("utf-8", errors="replace")
        return str(out or "")


def _write_cancel(process: Any) -> None:
    try:
        process.stdin.write(b"cancel\n")
        process.stdin.flush()
    except (OSError, ValueError, AttributeError):
        logger.info("Could not ask the scanner tool to cancel; it will be stopped")
        process.kill()


__all__ = [
    "LIST_TIMEOUT_S",
    "MISSING_SENTENCE",
    "SCAN_TIMEOUT_S",
    "TOOL_ENV",
    "TOOL_NAME",
    "WiaCliBackend",
    "find_tool",
    "parse_reply",
]
