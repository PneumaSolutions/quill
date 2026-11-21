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
Windows scanner tool's location.

In the Scribe PRD's terms it is a new entry point that feeds Import / Convert,
and so the Scribe tier: a scanned PDF that QUILL's OCR reads poorly gets the
same Scribe offer as any other import.

## How it is built

- `quill/core/scanning/` (wx-free): a `ScannerBackend` interface, the shared
  logic (choose a scanner, collect pages, make a PDF), and two backends.
- **Windows:** the open-source `wia-scan.exe` (Pneuma's open-scanner project,
  one Rust executable over Windows Image Acquisition 2.0), run through
  `safe_subprocess` with no console window and a timeout, on a worker thread,
  so a scanner driver that hangs takes the tool down, never QUILL. It prints one
  JSON object per command. Tested on a network HP LaserJet MFP.
- **macOS:** ImageCaptureCore through PyObjC
  (`pyobjc-framework-ImageCaptureCore`, an optional, darwin-only dependency).
  Untested on real hardware.
- `quill/ui/main_frame_scanning.py`: File > Import > Scan Pages and Choose Scanner, and
  `start_scan`, which any feature that wants pages can call.

## Questions for Jeff

1. Should the Convert with Scribe for Documents dialog take scanned pages
   directly (a "Scan pages" source), or is the Import route enough?
2. Should QUILL Lite get File > Import > Scan Pages too (rule 10: QUILL has it first)?
3. Is bundling `wia-scan.exe` in the installer's `tools\scanner` folder the
   right place?
