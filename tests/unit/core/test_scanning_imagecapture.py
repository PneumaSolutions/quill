"""The macOS backend over ImageCaptureCore, with fake frameworks.

The real backend is UNTESTED ON HARDWARE (see quill/core/scanning/imagecapture.py).
These fakes stand in for ImageCaptureCore, Foundation and objc and call the
delegate the way Apple documents: devices arrive through the browser, the
session opens, the flatbed is selected, and each scanned page is a URL.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from quill.core import scanning
from quill.core.scanning import imagecapture
from quill.core.scanning.base import ScanOptions


class _NSObject:
    @classmethod
    def alloc(cls):
        return cls.__new__(cls)

    def init(self):
        return self


class _Loop:
    def runMode_beforeDate_(self, *_a):  # noqa: N802
        return True


_FOUNDATION = SimpleNamespace(
    NSObject=_NSObject,
    NSRunLoop=SimpleNamespace(currentRunLoop=lambda: _Loop()),
    NSDefaultRunLoopMode="default",
    NSDate=SimpleNamespace(dateWithTimeIntervalSinceNow_=lambda s: s),
    NSURL=SimpleNamespace(fileURLWithPath_=lambda p: p),
)
_OBJC = SimpleNamespace(super=builtins.super, typedSelector=lambda _sig: lambda f: f)


class _Unit:
    def __init__(self) -> None:
        self.set: dict[str, object] = {}

    def supportedResolutions(self):  # noqa: N802
        return [75, 150, 300, 600, 1200]

    def physicalSize(self):  # noqa: N802
        return SimpleNamespace(width=8.5, height=11.7)

    def __getattr__(self, name):
        if name.startswith("set") and name.endswith("_"):
            return lambda value: self.set.__setitem__(name, value)
        raise AttributeError(name)


class _Scanner:
    def __init__(self, ident: str, name: str, folder_pages: int = 1, fail: str = "") -> None:
        self._id = ident
        self._name = name
        self.pages = folder_pages
        self.fail = fail
        self.delegate = None
        self.unit = _Unit()
        self.calls: list[str] = []
        self.folder = ""

    def persistentIDString(self):  # noqa: N802
        return self._id

    def name(self):
        return self._name

    def availableFunctionalUnitTypes(self):  # noqa: N802
        return [imagecapture.FUNCTIONAL_UNIT_FLATBED, imagecapture.FUNCTIONAL_UNIT_FEEDER]

    def setDelegate_(self, delegate):  # noqa: N802
        self.delegate = delegate

    def requestOpenSession(self):  # noqa: N802
        self.calls.append("open")
        self.delegate.device_didOpenSessionWithError_(self, None)

    def requestSelectFunctionalUnit_(self, unit):  # noqa: N802
        self.calls.append(f"select {unit}")
        self.delegate.scannerDevice_didSelectFunctionalUnit_error_(self, unit, None)

    def selectedFunctionalUnit(self):  # noqa: N802
        return self.unit

    def setTransferMode_(self, mode):  # noqa: N802
        self.calls.append(f"mode {mode}")

    def setDownloadsDirectory_(self, url):  # noqa: N802
        self.folder = url

    def setDocumentName_(self, _name):  # noqa: N802
        pass

    def setDocumentUTI_(self, uti):  # noqa: N802
        self.calls.append(uti)

    def requestScan(self):  # noqa: N802
        self.calls.append("scan")
        if self.fail:
            error = SimpleNamespace(localizedDescription=lambda: self.fail)
            self.delegate.scannerDevice_didCompleteScanWithError_(self, error)
            return
        for number in range(1, self.pages + 1):
            page = Path(self.folder) / f"scan {number}.png"
            page.write_bytes(b"png")
            self.delegate.scannerDevice_didScanToURL_(
                self, SimpleNamespace(path=lambda p=page: str(p))
            )
        self.delegate.scannerDevice_didCompleteScanWithError_(self, None)

    def cancelScan(self):  # noqa: N802
        self.calls.append("cancel")

    def requestCloseSession(self):  # noqa: N802
        self.calls.append("close")


def _ic(devices):
    class _Browser(_NSObject):
        def setDelegate_(self, delegate):  # noqa: N802
            self.delegate = delegate

        def setBrowsedDeviceTypeMask_(self, mask):  # noqa: N802
            self.mask = mask

        def start(self):
            for device in devices:
                self.delegate.deviceBrowser_didAddDevice_moreComing_(self, device, False)
            self.delegate.deviceBrowserDidEnumerateLocalDevices_(self)

        def stop(self):
            pass

    return SimpleNamespace(ICDeviceBrowser=_Browser)


@pytest.fixture(autouse=True)
def _fresh_delegate_class(monkeypatch):
    monkeypatch.setattr(imagecapture, "_DELEGATE_CLASS", None)


def _clock():
    now = [0.0]

    def tick():
        now[0] += 1.0
        return now[0]

    return tick


def _backend(devices, **kwargs):
    return imagecapture.ImageCaptureBackend(
        frameworks=(_ic(devices), _FOUNDATION, _OBJC), clock=_clock(), **kwargs
    )


def test_browsing_lists_every_scanner() -> None:
    found = _backend([_Scanner("u1", "Canon"), _Scanner("u2", "Epson")]).list_scanners()
    assert [(s.id, s.name, s.has_feeder) for s in found] == [
        ("u1", "Canon", True),
        ("u2", "Epson", True),
    ]


def test_a_scan_opens_selects_the_flatbed_configures_and_returns_pages(tmp_path) -> None:
    device = _Scanner("u1", "Canon", folder_pages=2)
    pages = _backend([device]).scan("u1", ScanOptions(300, "gray"), tmp_path)
    assert [p.name for p in pages] == ["scan 1.png", "scan 2.png"]
    assert device.calls[:2] == ["open", f"select {imagecapture.FUNCTIONAL_UNIT_FLATBED}"]
    assert "public.png" in device.calls and device.calls[-1] == "close"
    assert device.unit.set["setResolution_"] == 300
    assert device.unit.set["setPixelDataType_"] == imagecapture.PIXEL_DATA_GRAY
    assert device.unit.set["setScanArea_"] == (0, 0, 8.5, 11.7)


def test_color_and_an_unsupported_resolution_pick_the_nearest(tmp_path) -> None:
    device = _Scanner("u1", "Canon")
    _backend([device]).scan("u1", ScanOptions(400, "color"), tmp_path)
    assert device.unit.set["setResolution_"] == 600
    assert device.unit.set["setPixelDataType_"] == imagecapture.PIXEL_DATA_RGB


def test_the_main_thread_route_runs_every_step_through_it(tmp_path) -> None:
    ran = []

    def call_on_main(action):
        ran.append(action)
        action()

    device = _Scanner("u1", "Canon")
    _backend([device], call_on_main=call_on_main).scan("u1", ScanOptions(), tmp_path)
    assert len(ran) >= 5  # start browser, open, select, scan, close, stop


def test_a_missing_scanner_is_a_sentence(tmp_path) -> None:
    with pytest.raises(scanning.NoScannerFound):
        _backend([_Scanner("u1", "Canon")]).scan("other", ScanOptions(), tmp_path)


def test_a_scan_error_is_reported_and_the_session_closed(tmp_path) -> None:
    device = _Scanner("u1", "Canon", fail="The lid is open.")
    with pytest.raises(scanning.ScanError, match="lid is open"):
        _backend([device]).scan("u1", ScanOptions(), tmp_path)
    assert device.calls[-1] == "close"


def test_without_pyobjc_scanning_says_so(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "ImageCaptureCore", None)
    with pytest.raises(scanning.ScannerToolMissing, match="ImageCaptureCore"):
        imagecapture.ImageCaptureBackend().list_scanners()


def test_resolutions_from_an_index_set_shape() -> None:
    class _IndexSet:
        def __init__(self, values):
            self.values = values

        def firstIndex(self):  # noqa: N802
            return self.values[0]

        def indexGreaterThanIndex_(self, index):  # noqa: N802
            later = [v for v in self.values if v > index]
            return later[0] if later else 0x7FFFFFFFFFFFFFFF

    assert imagecapture.nearest_resolution(_IndexSet([100, 200, 300]), 250) == 300
    assert imagecapture.nearest_resolution(_IndexSet([100, 200]), 600) == 200
