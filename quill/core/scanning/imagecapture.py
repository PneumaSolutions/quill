"""macOS scanning through Apple's ImageCaptureCore (PyObjC).

**Untested on real hardware.** Written 2026-11-21 against Apple's
ImageCaptureCore documentation and exercised only with the fakes in
``tests/unit/core/scanning/test_imagecapture.py``; nobody has scanned a page
with it yet. Treat the first run on a Mac with a scanner as the real test.

How it works: an ``ICDeviceBrowser`` looks for scanners (local, shared,
Bonjour and AirScan/eSCL network ones) for a few seconds; to scan, QUILL
opens a session on the chosen ``ICScannerDevice``, selects the flatbed (or
the document feeder when there is no flatbed), sets the resolution, gray or
color, and the whole glass as the scan area, and asks for a file-based scan
into a temporary folder as PNG. Each ``didScanToURL`` is one page; the scan
is over at ``didCompleteScanWithError``.

Threading: ImageCaptureCore calls its delegates on the run loop of the
thread that started the browser. QUILL calls :meth:`list_scanners` and
:meth:`scan` from a worker thread, and by default this module pumps that
thread's own run loop while it waits. A host that wants the work on the main
thread (where Apple's examples do it) passes ``call_on_main`` -- the UI hands
in ``wx.CallAfter`` -- and the worker then waits on an event while wx's main
loop delivers the callbacks.

The dependency is optional and macOS-only: ``pyobjc-framework-ImageCaptureCore``
in the ``mac`` extra with a ``sys_platform == 'darwin'`` marker (the full
``pyobjc`` in the ``macos`` build extra includes it too). Without it,
scanning says so in a sentence. Nothing is imported at module load.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quill.core.scanning.base import (
    COLOR_COLOR,
    CancelFn,
    NoScannerFound,
    ScanCancelled,
    ScanError,
    ScannerBackend,
    ScannerInfo,
    ScannerToolMissing,
    ScanOptions,
)

#: ImageCaptureCore constants (ICCommonConstants.h / ICScannerFunctionalUnits.h).
IC_DEVICE_TYPE_MASK_SCANNER = 0x00000002
IC_DEVICE_LOCATION_MASK_ALL = 0x00000100 | 0x00000200 | 0x00000400 | 0x00000800 | 0x0000FE00
FUNCTIONAL_UNIT_FLATBED = 0
FUNCTIONAL_UNIT_FEEDER = 3
PIXEL_DATA_GRAY = 1
PIXEL_DATA_RGB = 2
BIT_DEPTH_8 = 8
TRANSFER_MODE_FILE = 0
MEASUREMENT_INCHES = 0
PNG_UTI = "public.png"

BROWSE_SECONDS = 4.0
SESSION_TIMEOUT_S = 30.0
SCAN_TIMEOUT_S = 600.0

MISSING_SENTENCE = (
    "Scanning on this Mac needs Apple's Image Capture support for Python "
    "(pyobjc-framework-ImageCaptureCore), which this copy of QUILL does not have."
)


def load_frameworks() -> tuple[Any, Any, Any]:
    """``(ImageCaptureCore, Foundation, objc)``, or :class:`ScannerToolMissing`."""
    try:
        import Foundation  # type: ignore[import-not-found]
        import ImageCaptureCore  # type: ignore[import-not-found]
        import objc  # type: ignore[import-not-found]
    except ImportError:
        raise ScannerToolMissing(MISSING_SENTENCE) from None
    return ImageCaptureCore, Foundation, objc


class _Waiter:
    """Collects delegate callbacks and lets the worker wait for them."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.devices: list[Any] = []
        self.enumerated = False
        self.session_open = False
        self.unit_selected = False
        self.scanned: list[Path] = []
        self.complete = False
        self.error = ""

    def note(self) -> None:
        self.event.set()


def _error_text(error: Any) -> str:
    if error is None:
        return ""
    try:
        return str(error.localizedDescription())
    except Exception:  # noqa: BLE001 - a fake or odd NSError still says something
        return str(error)


_DELEGATE_CLASS: Any = None


def delegate_class(objc: Any, foundation: Any) -> Any:
    """The Objective-C delegate class, defined once per process.

    One object serves as the browser, device and scanner delegate; it
    forwards every callback to the ``_Waiter`` it is given.
    """
    global _DELEGATE_CLASS
    if _DELEGATE_CLASS is not None:
        return _DELEGATE_CLASS

    typed = objc.typedSelector

    class QuillScanDelegate(foundation.NSObject):  # type: ignore[misc,name-defined]
        # Explicit Objective-C signatures: "Z" is the BOOL moreComing/moreGoing
        # argument, which PyObjC must not treat as an object.
        waiter: _Waiter

        # ICDeviceBrowserDelegate
        @typed(b"v@:@@Z")
        def deviceBrowser_didAddDevice_moreComing_(self, _b: Any, device: Any, _m: Any) -> None:  # noqa: N802
            with self.waiter.lock:
                self.waiter.devices.append(device)
            self.waiter.note()

        @typed(b"v@:@@Z")
        def deviceBrowser_didRemoveDevice_moreGoing_(self, _b: Any, device: Any, _m: Any) -> None:  # noqa: N802
            with self.waiter.lock:
                if device in self.waiter.devices:
                    self.waiter.devices.remove(device)

        @typed(b"v@:@")
        def deviceBrowserDidEnumerateLocalDevices_(self, _b: Any) -> None:  # noqa: N802
            self.waiter.enumerated = True
            self.waiter.note()

        # ICDeviceDelegate
        @typed(b"v@:@")
        def didRemoveDevice_(self, _device: Any) -> None:  # noqa: N802
            self.waiter.error = "The scanner went away."
            self.waiter.note()

        @typed(b"v@:@@")
        def device_didOpenSessionWithError_(self, _device: Any, error: Any) -> None:  # noqa: N802
            self.waiter.error = _error_text(error)
            self.waiter.session_open = not self.waiter.error
            self.waiter.note()

        @typed(b"v@:@@")
        def device_didCloseSessionWithError_(self, _device: Any, _error: Any) -> None:  # noqa: N802
            self.waiter.note()

        @typed(b"v@:@")
        def deviceDidBecomeReady_(self, _device: Any) -> None:  # noqa: N802
            self.waiter.note()

        # ICScannerDeviceDelegate
        @typed(b"v@:@@@")
        def scannerDevice_didSelectFunctionalUnit_error_(  # noqa: N802
            self, _s: Any, _unit: Any, error: Any
        ) -> None:
            self.waiter.error = _error_text(error)
            self.waiter.unit_selected = not self.waiter.error
            self.waiter.note()

        @typed(b"v@:@@")
        def scannerDevice_didScanToURL_(self, _scanner: Any, url: Any) -> None:  # noqa: N802
            with self.waiter.lock:
                self.waiter.scanned.append(Path(str(url.path())))
            self.waiter.note()

        @typed(b"v@:@@")
        def scannerDevice_didCompleteScanWithError_(self, _scanner: Any, error: Any) -> None:  # noqa: N802
            self.waiter.error = _error_text(error)
            self.waiter.complete = True
            self.waiter.note()

    _DELEGATE_CLASS = QuillScanDelegate
    return _DELEGATE_CLASS


def device_id(device: Any) -> str:
    """A stable id for a device: its persistent id, else its name."""
    for attribute in ("persistentIDString", "UUIDString"):
        getter = getattr(device, attribute, None)
        if callable(getter):
            try:
                value = getter()
            except Exception:  # noqa: BLE001
                value = None
            if value:
                return str(value)
    return str(device.name())


def nearest_resolution(supported: Any, wanted: int) -> int:
    """The supported resolution closest to *wanted* (prefer the next higher)."""
    values: list[int] = []
    try:
        index = supported.firstIndex()
        not_found = 0x7FFFFFFFFFFFFFFF
        while index != not_found and len(values) < 10000:
            values.append(int(index))
            index = supported.indexGreaterThanIndex_(index)
    except Exception:  # noqa: BLE001 - a plain Python iterable (tests) also works
        values = [int(v) for v in supported]
    if not values:
        return wanted
    higher = [v for v in values if v >= wanted]
    return min(higher) if higher else max(values)


class ImageCaptureBackend(ScannerBackend):
    """:class:`ScannerBackend` over ImageCaptureCore. Untested on hardware."""

    name = "Image Capture"

    def __init__(
        self,
        *,
        frameworks: tuple[Any, Any, Any] | None = None,
        call_on_main: Callable[[Callable[[], None]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        browse_seconds: float = BROWSE_SECONDS,
    ) -> None:
        self._frameworks = frameworks
        self._call_on_main = call_on_main
        self._clock = clock
        self._browse_seconds = browse_seconds

    # ---------------------------------------------------------- plumbing

    def _fw(self) -> tuple[Any, Any, Any]:
        if self._frameworks is None:
            self._frameworks = load_frameworks()
        return self._frameworks

    def _on_loop(self, action: Callable[[], None]) -> None:
        """Run *action* where ImageCaptureCore's callbacks will arrive."""
        if self._call_on_main is None:
            action()
            return
        done = threading.Event()
        failure: list[BaseException] = []

        def _wrapped() -> None:
            try:
                action()
            except BaseException as exc:  # noqa: BLE001 - re-raised on the worker
                failure.append(exc)
            finally:
                done.set()

        self._call_on_main(_wrapped)
        if not done.wait(SESSION_TIMEOUT_S):
            raise ScanError("The Mac's Image Capture service did not answer. Try again.")
        if failure:
            raise failure[0]

    def _wait(
        self,
        waiter: _Waiter,
        until: Callable[[], bool],
        timeout: float,
        cancel_requested: CancelFn | None = None,
    ) -> bool:
        """Wait for *until*; True when it came true, False on timeout."""
        _ic, foundation, _objc = self._fw()
        deadline = self._clock() + timeout
        while not until():
            if waiter.error:
                return False
            if cancel_requested is not None and cancel_requested():
                raise ScanCancelled("Scan stopped.")
            if self._clock() >= deadline:
                return False
            if self._call_on_main is None:
                loop = foundation.NSRunLoop.currentRunLoop()
                loop.runMode_beforeDate_(
                    foundation.NSDefaultRunLoopMode,
                    foundation.NSDate.dateWithTimeIntervalSinceNow_(0.1),
                )
            else:
                waiter.event.wait(0.1)
                waiter.event.clear()
        return True

    def _browse(self, waiter: _Waiter, stop_when: Callable[[], bool]) -> Any:
        ic, foundation, objc = self._fw()
        delegate = delegate_class(objc, foundation).alloc().init()
        delegate.waiter = waiter  # a Python attribute on the PyObjC instance
        holder: dict[str, Any] = {}

        def _start() -> None:
            browser = ic.ICDeviceBrowser.alloc().init()
            browser.setDelegate_(delegate)
            browser.setBrowsedDeviceTypeMask_(
                IC_DEVICE_TYPE_MASK_SCANNER | IC_DEVICE_LOCATION_MASK_ALL
            )
            browser.start()
            holder["browser"] = browser

        self._on_loop(_start)
        self._wait(waiter, stop_when, self._browse_seconds)
        return holder.get("browser"), delegate

    def _stop(self, browser: Any) -> None:
        if browser is not None:
            self._on_loop(browser.stop)

    # ---------------------------------------------------------- interface

    def list_scanners(self) -> list[ScannerInfo]:
        waiter = _Waiter()
        browser, _delegate = self._browse(waiter, lambda: False)
        try:
            with waiter.lock:
                devices = list(waiter.devices)
            return [ScannerInfo(device_id(d), str(d.name()), _has_feeder(d)) for d in devices]
        finally:
            self._stop(browser)

    def scan(
        self,
        device_id_: str,
        options: ScanOptions,
        out_dir: Path,
        *,
        cancel_requested: CancelFn | None = None,
    ) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        waiter = _Waiter()

        def _found() -> bool:
            with waiter.lock:
                return any(device_id(d) == device_id_ for d in waiter.devices)

        browser, delegate = self._browse(waiter, _found)
        try:
            with waiter.lock:
                matches = [d for d in waiter.devices if device_id(d) == device_id_]
            if not matches:
                raise NoScannerFound(
                    "QUILL could not find that scanner. Check it is on, then try again."
                )
            return self._scan_device(
                matches[0], delegate, waiter, options, out_dir, cancel_requested
            )
        finally:
            self._stop(browser)

    def _scan_device(
        self,
        device: Any,
        delegate: Any,
        waiter: _Waiter,
        options: ScanOptions,
        out_dir: Path,
        cancel_requested: CancelFn | None,
    ) -> list[Path]:
        _ic, foundation, _objc = self._fw()

        def _open() -> None:
            device.setDelegate_(delegate)
            device.requestOpenSession()

        self._on_loop(_open)
        if not self._wait(waiter, lambda: waiter.session_open, SESSION_TIMEOUT_S):
            raise ScanError(_say(waiter, "The scanner did not open. Is it busy?"))
        try:
            unit_type = _preferred_unit(device)

            def _select() -> None:
                device.requestSelectFunctionalUnit_(unit_type)

            self._on_loop(_select)
            if not self._wait(waiter, lambda: waiter.unit_selected, SESSION_TIMEOUT_S):
                raise ScanError(_say(waiter, "The scanner did not get ready."))

            def _configure_and_scan() -> None:
                unit = device.selectedFunctionalUnit()
                _configure(unit, options)
                device.setTransferMode_(TRANSFER_MODE_FILE)
                device.setDownloadsDirectory_(foundation.NSURL.fileURLWithPath_(str(out_dir)))
                device.setDocumentName_("scan")
                device.setDocumentUTI_(PNG_UTI)
                device.requestScan()

            self._on_loop(_configure_and_scan)
            try:
                finished = self._wait(
                    waiter, lambda: waiter.complete, SCAN_TIMEOUT_S, cancel_requested
                )
            except ScanCancelled:
                self._on_loop(device.cancelScan)
                raise
            if waiter.error:
                raise ScanError(f"The scan failed: {waiter.error}")
            if not finished:
                raise ScanError("The scan took too long and was stopped. Try again.")
            pages = [p for p in waiter.scanned if p.is_file()]
            if not pages:
                raise ScanError("The scanner finished but sent no page. Try again.")
            return pages
        finally:
            self._on_loop(device.requestCloseSession)


def _say(waiter: _Waiter, fallback: str) -> str:
    return f"{fallback} ({waiter.error})" if waiter.error else fallback


def _has_feeder(device: Any) -> bool:
    try:
        return FUNCTIONAL_UNIT_FEEDER in [int(t) for t in device.availableFunctionalUnitTypes()]
    except Exception:  # noqa: BLE001
        return False


def _preferred_unit(device: Any) -> int:
    """The flatbed when there is one, else the feeder, else what it offers."""
    try:
        available = [int(t) for t in device.availableFunctionalUnitTypes()]
    except Exception:  # noqa: BLE001
        available = []
    for unit in (FUNCTIONAL_UNIT_FLATBED, FUNCTIONAL_UNIT_FEEDER):
        if unit in available:
            return unit
    return available[0] if available else FUNCTIONAL_UNIT_FLATBED


def _configure(unit: Any, options: ScanOptions) -> None:
    """Resolution, gray or color, 8 bits, and the whole glass."""
    unit.setMeasurementUnit_(MEASUREMENT_INCHES)
    unit.setResolution_(nearest_resolution(unit.supportedResolutions(), options.dpi))
    unit.setPixelDataType_(PIXEL_DATA_RGB if options.color == COLOR_COLOR else PIXEL_DATA_GRAY)
    unit.setBitDepth_(BIT_DEPTH_8)
    size = unit.physicalSize()
    try:
        from Foundation import NSMakeRect  # type: ignore[import-not-found]

        area = NSMakeRect(0, 0, size.width, size.height)
    except ImportError:  # the fakes in the tests
        area = (0, 0, size.width, size.height)
    unit.setScanArea_(area)


__all__ = [
    "MISSING_SENTENCE",
    "ImageCaptureBackend",
    "delegate_class",
    "device_id",
    "load_frameworks",
    "nearest_resolution",
]
