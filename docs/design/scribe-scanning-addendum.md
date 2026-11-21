# Proposal: scanning as a QUILL entry point (addendum to the Scribe PRD)

For Jeff's review. Mike asked for scanning everywhere in QUILL, not only for
Scribe. This is built and can be changed or removed without touching Scribe.

## What it is

**File > Import > Scan Pages** (QUILL key, then E with Shift) scans one or more pages
from a flatbed or document-feeder scanner, asking "Scan another page?" after
each, then offers what to do with them: read the text with QUILL's own OCR
(the Import / Convert Document route), insert the page into the document as a
picture (Insert Image, with the alt text field focused), or save one PDF or
the pictures. **Tools > Choose Scanner** picks the scanner; with one, QUILL uses
it silently. **Options > Scanning** has the resolution, gray or color, and the
scanner tool's location.

In the Scribe PRD's terms it is a new entry point that feeds Import / Convert,
and so the Scribe tier: a scanned PDF that QUILL's OCR reads poorly gets the
same Scribe offer as any other import.

## How it is built

- `quill/core/scanning/` (wx-free): a `ScannerBackend` interface, the shared
  logic (choose a scanner, collect pages, make a PDF), and one backend,
  `pagescan_cli.py`, used on Windows and macOS alike.
- **The scanner tool is pagescan** (Pneuma Solutions, MIT,
  [PneumaSolutions/pagescan](https://github.com/PneumaSolutions/pagescan)), one
  small Rust executable with no runtime dependencies: Windows Image Acquisition
  2.0 with a WIA 1.0 fallback on Windows, Apple's ImageCaptureCore on macOS.
  QUILL runs it through `safe_subprocess` with no console window and a timeout,
  on a worker thread, so a scanner driver that hangs takes the tool down, never
  QUILL. On a Mac the separate process also gives ImageCaptureCore the main
  thread it needs, so no PyObjC is involved. It prints one JSON object per
  command (its `docs/protocol.md`); QUILL ignores anything on stderr and passes
  the tool's `error` sentence on to the listener.
- **Source by kind.** Every scan passes `--source flatbed` or `--source feeder`,
  never a source id, because Windows source ids can change between runs: the
  glass when the scanner has one, else the feeder.
- **Where QUILL finds it,** first match wins: Options > Scanning (a file or a
  folder), `QUILL_SCANNER_TOOL`, `tools\scanner` beside an installed QUILL,
  `Contents/Resources/tools/scanner` in the Mac app, `tools/scanner` in a source
  checkout, the data folder, then `PATH`. On Windows each place also accepts the
  tool's old name, `wia-scan.exe`, so a computer set up before the rename keeps
  scanning.
- **Bundling.** The Windows build copies `pagescan.exe` into
  `portable\tools\scanner` (`--scanner-tool`, or `tools\scanner\pagescan.exe`
  in the checkout), and the Mac build puts `tools/scanner/pagescan` into the
  app's Resources and signs it. The binary is prebuilt: download it from the
  pagescan GitHub release, or build it with `cargo build --release` (for
  Windows from Linux, `--target x86_64-pc-windows-gnu`). A build without it
  still works and Scan Pages says the tool is missing.
- **Tested** on Windows with a network HP Color LaserJet Pro MFP 3301 (eSCL).
  The macOS side of pagescan has not yet been run on a Mac with a scanner.
- `quill/ui/main_frame_scanning.py`: File > Import > Scan Pages and Choose Scanner, and
  `start_scan`, which any feature that wants pages can call.

## Questions for Jeff

1. Should the Convert with Scribe for Documents dialog take scanned pages
   directly (a "Scan pages" source), or is the Import route enough?
2. Should QUILL Lite get File > Import > Scan Pages too (rule 10: QUILL has it first)?
3. Is bundling `pagescan.exe` in the installer's `tools\scanner` folder (and
   `pagescan` in the Mac app's Resources) the right place?
