"""Scanning on Windows and macOS through the ``pagescan`` command-line tool.

``pagescan`` (Pneuma Solutions, MIT, https://github.com/PneumaSolutions/pagescan)
is one small executable with no runtime dependencies: Windows Image Acquisition
(WIA 2.0, falling back to WIA 1.0) on Windows, Apple's ImageCaptureCore on
macOS. It does the device work in its own process, so a driver that hangs or
crashes takes the tool down, never QUILL -- and on macOS the process has its
own main thread, which ImageCaptureCore needs. The contract (the tool's
``docs/protocol.md``, version 1), one JSON object on stdout per command:

* ``pagescan list`` ->
  ``{"ok": true, "scanners": [{"id": ..., "name": ..., "sources":
  [{"id": ..., "kind": "flatbed"|"feeder", "duplex": bool}], "error": ...}]}``.
  A scanner whose driver did not answer is still listed, with no sources and
  an ``error``; QUILL keeps it as unavailable (``ScannerInfo.error``). One
  physical scanner is listed once, so an id saved earlier may be gone.
* ``pagescan scan --device ID --source flatbed|feeder --dpi 300 --gray|--color
  --out DIR --format png`` -> ``{"ok": true, "files": [...], "error": null}``;
  writing ``cancel`` to its stdin stops a running scan, keeping finished pages.

QUILL always names the source by kind (``--source flatbed`` or ``--source
feeder``), never by a source ``id``: on Windows those ids can change between
runs. On failure ``ok`` is false and ``error`` is a sentence written to be read
aloud, which QUILL passes on. Lines on stderr are not part of the answer and are
ignored. The tool runs with no console window (``CREATE_NO_WINDOW``, through
:mod:`quill.stability.safe_subprocess`), with a timeout, and only from a worker
thread.

Where the tool is found, first match wins: ``{app}/tools/scanner/`` beside an
installed QUILL (``QUILL_APP_ROOT``), ``Contents/Resources/tools/scanner/`` in
a macOS app (``RESOURCEPATH``), ``tools/scanner/`` in a source checkout, then
the path in Options (Scanning tab, ``scanner_tool_path``, a file or a folder),
the ``QUILL_SCANNER_TOOL`` environment variable, then ``<QUILL
data>/tools/scanner/``. The bundled copy comes first, so a setting can only
stand in for a missing tool, never replace the one QUILL ships (independent
review Q2). A configured path or the variable must be absolute and on this
computer -- never a UNC share or a mapped network drive -- and a file must be
named ``pagescan`` (or ``wia-scan.exe``); ``scanner_tool_path`` is a
machine-local setting, so a portable settings import never carries it.
``PATH`` and the current folder are never searched, so a stray ``pagescan``
elsewhere can never run in its place (docs/design/scribe-security-review.md).
The scan's answer is trusted only for files inside the folder QUILL gave it,
since those pages may go on to OCR or to Scribe. In each folder ``pagescan.exe`` (on a
Mac, ``pagescan``) is looked for first, then the tool's old Windows name
``wia-scan.exe``, so a computer set up before the rename keeps scanning. It is
local-only: nothing here touches the network.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from quill.core.scanning.base import (
    COLOR_COLOR,
    SOURCE_FEEDER,
    SOURCE_FLATBED,
    CancelFn,
    ScanCancelled,
    ScanError,
    ScannerBackend,
    ScannerInfo,
    ScannerToolMissing,
    ScanOptions,
)

logger = logging.getLogger(__name__)

TOOL_STEM = "pagescan"
#: The tool's name before 2026-11-21, still found so an existing install scans.
LEGACY_TOOL_NAME = "wia-scan.exe"
TOOL_ENV = "QUILL_SCANNER_TOOL"
#: ``list`` answers within about 15 seconds even when a driver hangs (its
#: protocol.md, "Timing and unresponsive drivers"); this leaves room for a slow
#: first start without leaving a listener waiting minutes on a stuck tool.
LIST_TIMEOUT_S = 45.0
SCAN_TIMEOUT_S = 900.0
#: The tool's own limit, a little inside QUILL's, so it stops cleanly and
#: reports the pages it saved before QUILL has to kill it.
TOOL_TIMEOUT_S = 870
_POLL_S = 0.2

MISSING_SENTENCE = (
    "QUILL's scanner tool (pagescan) is not installed. Reinstall QUILL, or "
    "set its location in Options, Scanning."
)


def tool_names(platform: str | None = None) -> tuple[str, ...]:
    """The file names the tool may have here, preferred first."""
    if (platform or sys.platform) == "win32":
        return (f"{TOOL_STEM}.exe", LEGACY_TOOL_NAME)
    return (TOOL_STEM,)


_DRIVE_REMOTE = 4  # GetDriveTypeW: a mapped network drive


def _drive_is_remote(drive: str) -> bool:
    """True for a mapped network drive such as ``Z:`` (Windows only)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kind = ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - cannot tell: treat it as not local
        return True
    return int(kind) == _DRIVE_REMOTE


def is_local_path(text: str, platform: str | None = None) -> bool:
    """An absolute path on this computer: never a UNC share or a network drive."""
    if text.startswith(("\\\\", "//")):
        return False  # \\server\share, \\?\UNC\..., //server/share
    if (platform or sys.platform) == "win32":
        import ntpath

        drive, rest = ntpath.splitdrive(text)
        if not (len(drive) == 2 and drive[0].isalpha() and rest[:1] in ("\\", "/")):
            return False
        return not _drive_is_remote(drive)
    return text.startswith("/")


def _explicit(value: str) -> Path | None:
    """A configured file or folder, only when it is an absolute path on this computer."""
    cleaned = value.strip().strip('"')
    if not cleaned or not is_local_path(cleaned):
        return None
    path = Path(cleaned)
    return path if path.is_absolute() else None


def _places(configured: str, env: Mapping[str, str]) -> list[tuple[Path, bool]]:
    """Where to look, in order, each with whether it may name the file itself."""
    places: list[tuple[Path, bool]] = []
    for variable in ("QUILL_APP_ROOT", "RESOURCEPATH"):
        root = env.get(variable, "").strip()
        if root and Path(root).is_absolute():
            places.append((Path(root) / "tools" / "scanner", False))
    # A run from source: the gitignored tools/scanner folder of the checkout.
    places.append((Path(__file__).resolve().parents[3] / "tools" / "scanner", False))
    chosen = _explicit(configured)
    if chosen is not None:
        places.append((chosen, True))
    variable_path = _explicit(env.get(TOOL_ENV, ""))
    if variable_path is not None:
        places.append((variable_path, True))
    try:
        from quill.core.paths import app_data_dir

        places.append((app_data_dir() / "tools" / "scanner", False))
    except Exception:  # noqa: BLE001 - no data folder: the other places still count
        pass
    return places


def find_tool(
    configured: str = "",
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path | None:
    """The first ``pagescan`` (or legacy ``wia-scan.exe``) that exists, or ``None``."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    names = tool_names(platform)
    for place, may_be_file in _places(configured, env):
        if may_be_file and place.is_file():
            if place.name.lower() in {n.lower() for n in names}:
                return place
            continue  # a file by any other name is not the scanner tool
        if place.is_dir():
            for name in names:
                if (place / name).is_file():
                    return place / name
    return None


Runner = Callable[[list[str], float], tuple[int, str]]


def _run(args: list[str], timeout: float) -> tuple[int, str]:
    from quill.stability.safe_subprocess import run_subprocess_safely

    result = run_subprocess_safely(args, timeout_seconds=timeout)
    return result.returncode, result.stdout or ""


def parse_reply(stdout: str) -> dict[str, Any]:
    """The tool's JSON answer: the last stdout line that parses as an object."""
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


def _kinds(sources: Any) -> set[str]:
    return {str(s.get("kind", "")).lower() for s in sources or () if isinstance(s, dict)}


class PagescanBackend(ScannerBackend):
    """:class:`ScannerBackend` over the ``pagescan`` executable."""

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
            kinds = _kinds(item.get("sources"))
            scanners.append(
                ScannerInfo(
                    str(item["id"]),
                    str(item.get("name") or "Scanner"),
                    has_feeder=SOURCE_FEEDER in kinds,
                    # Sources unread (an "error" on this scanner): assume the glass.
                    has_flatbed=SOURCE_FLATBED in kinds or not kinds,
                    error=str(item.get("error") or "").strip(),
                )
            )
        return scanners

    def scan_args(
        self,
        device_id: str,
        options: ScanOptions,
        out_dir: Path,
        source: str = SOURCE_FLATBED,
    ) -> list[str]:
        return [
            str(self.tool()),
            "scan",
            "--device",
            device_id,
            "--source",
            SOURCE_FEEDER if source == SOURCE_FEEDER else SOURCE_FLATBED,
            "--dpi",
            str(options.dpi),
            "--color" if options.color == COLOR_COLOR else "--gray",
            "--out",
            str(out_dir),
            "--format",
            "png",
            "--timeout",
            str(TOOL_TIMEOUT_S),
        ]

    def scan(
        self,
        device_id: str,
        options: ScanOptions,
        out_dir: Path,
        *,
        source: str = SOURCE_FLATBED,
        cancel_requested: CancelFn | None = None,
    ) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        args = self.scan_args(device_id, options, out_dir, source)
        try:
            stdout = self._scan_process(args, cancel_requested)
        except ScanTimedOut:
            # The tool was stopped, but the pages it had saved are still in
            # the folder: keep them (independent review Q21).
            kept = sorted(p for p in out_dir.glob("*.png") if p.is_file())
            if kept:
                return kept
            raise
        reply = parse_reply(stdout)
        files = [Path(str(f)) for f in reply.get("files") or () if str(f).strip()]
        files = [f for f in files if f.is_file() and _inside(f, out_dir)]
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
                # stderr goes nowhere: nobody reads it, and a full pipe would
                # block the tool mid-scan (Q21). stdout is drained as it comes.
                process = spawn_subprocess_safely(args, stderr=subprocess.DEVNULL)
            except OSError as exc:
                raise ScannerToolMissing(f"{MISSING_SENTENCE} ({exc})") from None
        drained = _drain(process)
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
                raise ScanTimedOut("The scan took too long and was stopped. Try again.")
            self._sleep(_POLL_S)
        out = drained() if drained is not None else process.communicate()[0]
        if cancelled:
            raise ScanCancelled("Scan stopped.")
        if isinstance(out, bytes):
            return out.decode("utf-8", errors="replace")
        return str(out or "")


class ScanTimedOut(ScanError):
    """The tool ran past its time and was stopped (its saved pages are kept)."""

    code = "QUILL-SCAN-DEVICE-TIMEOUT"


def _drain(process: Any) -> Callable[[], bytes] | None:
    """Read the tool's stdout on a thread as it comes, so it can never block on
    a full pipe. Returns a function giving everything read once it ended."""
    import threading

    stream = getattr(process, "stdout", None)
    if stream is None or not hasattr(stream, "read"):
        return None
    chunks: list[bytes] = []

    def _read() -> None:
        try:
            for chunk in iter(lambda: stream.read(65536), b""):
                chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=_read, name="pagescan-stdout", daemon=True)
    reader.start()

    def _result() -> bytes:
        process.wait()
        reader.join(timeout=5.0)
        return b"".join(chunks)

    return _result


def _inside(path: Path, folder: Path) -> bool:
    """True when ``path`` is in ``folder``: the tool may only hand back its pages."""
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False


def _write_cancel(process: Any) -> None:
    try:
        process.stdin.write(b"cancel\n")
        process.stdin.flush()
    except (OSError, ValueError, AttributeError):
        logger.info("Could not ask the scanner tool to cancel; it will be stopped")
        process.kill()


__all__ = [
    "LEGACY_TOOL_NAME",
    "LIST_TIMEOUT_S",
    "MISSING_SENTENCE",
    "SCAN_TIMEOUT_S",
    "TOOL_ENV",
    "TOOL_STEM",
    "TOOL_TIMEOUT_S",
    "PagescanBackend",
    "find_tool",
    "is_local_path",
    "parse_reply",
    "tool_names",
]
