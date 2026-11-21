"""Scan pages from a scanner, anywhere in QUILL (MainFrame mixin).

File > Import > Scan Pages... scans one or more pages and then asks what to do with
them: read the text (QUILL's own OCR, the same route as Import / Convert
Document), insert a page into the document as an image, or save them as a
PDF or as pictures. Tools > Choose Scanner... picks which scanner QUILL
uses.

Every caller that wants pages goes through :meth:`ScanningMixin.start_scan`,
which runs the shared logic in :mod:`quill.core.scanning`:

* the scanner list and every scan run on the background task pool, so the
  window (and the screen reader) stays responsive while a network scanner
  takes half a minute;
* one scanner is used without asking; with several, QUILL asks once and
  remembers the choice in Settings (``scanner_device``);
* after each page: "Page 1 scanned. Scan another page?" -- Yes scans the
  next, No hands every page over in one go.

Wiring expectations from MainFrame: ``_wx``, ``frame``, ``settings``,
``_task_manager``, ``_show_modal_dialog``, ``_show_message_box``,
``_set_status``, ``_announce``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

_TITLE = "Scan Pages"

#: What File > Import > Scan Pages offers once the pages are in, in order.
USE_OCR = "ocr"
USE_INSERT = "insert"
USE_PDF = "pdf"
USE_IMAGES = "images"
SCAN_USES: tuple[tuple[str, str], ...] = (
    (USE_OCR, "Read the text (OCR) and open it in QUILL"),
    (USE_INSERT, "Insert the page into this document as a picture"),
    (USE_PDF, "Save as a PDF"),
    (USE_IMAGES, "Save as pictures (PNG)"),
)


class ScanningMixin:
    """``file.scan`` and ``file.scan_choose_scanner``, plus :meth:`start_scan`."""

    _scan_busy: bool = False

    # ------------------------------------------------------------- wiring

    def _register_scanning_commands(self) -> None:
        host: Any = self
        host.commands.register(
            "file.scan", "Scan Pages...", self.scan_pages, host._binding_for("file.scan")
        )
        host.commands.register(
            "file.scan_choose_scanner",
            "Choose Scanner...",
            self.choose_scanner,
            host._binding_for("file.scan_choose_scanner"),
        )

    def _append_scan_rows(self, import_menu: Any) -> None:
        """File > Import > Scan Pages..., right after Import / Convert Document.

        In Import because a scan is a document arriving, and because the File
        menu itself has more rows than letters. Choose Scanner is in Tools,
        where there is room (``_append_choose_scanner_row``).
        """
        from quill.core.i18n import _

        wx = self._wx
        host: Any = self
        self._id_scan_pages = wx.NewIdRef()
        label = host._menu_label(_("S&can Pages..."), "file.scan")
        import_menu.Insert(1, self._id_scan_pages, label)
        host.frame.Bind(wx.EVT_MENU, lambda _e: self.scan_pages(), id=self._id_scan_pages)

    def _append_choose_scanner_row(self, tools_menu: Any) -> None:
        """Tools > Choose Scanner..., beside Scribe for Documents."""
        from quill.core.i18n import _

        wx = self._wx
        host: Any = self
        self._id_choose_scanner = wx.NewIdRef()
        label = host._menu_label(_("Choose Scanne&r..."), "file.scan_choose_scanner")
        tools_menu.Append(self._id_choose_scanner, label)
        host.frame.Bind(wx.EVT_MENU, lambda _e: self.choose_scanner(), id=self._id_choose_scanner)

    # ------------------------------------------------------------ commands

    def scan_pages(self) -> None:
        """Scan, then ask what to do with the pages."""
        self.start_scan(self._scan_offer_uses)

    def choose_scanner(self) -> None:
        """List the scanners and remember the one picked."""

        def _picked(scanners: list[Any]) -> None:
            from quill.core.scanning import NO_SCANNER_SENTENCE

            if not scanners:
                self._scan_say_problem(NO_SCANNER_SENTENCE)
                return
            scanner = self._scan_ask_which(scanners, force=True)
            if scanner is not None:
                self._announce(f"QUILL will scan with {scanner.name}.")

        self._scan_list(_picked)

    # -------------------------------------------------------------- the flow

    def start_scan(
        self,
        on_done: Callable[[Any], None],
        *,
        parent: Any = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        """Scan pages and call ``on_done(session)`` with a ScanSession.

        The caller owns the session and calls ``session.cleanup()`` when it is
        finished with the files. ``on_cancel`` runs when nothing was scanned.
        """
        if self._scan_busy:
            self._set_status("QUILL is already scanning. One scan at a time.")
            return

        def _listed(scanners: list[Any]) -> None:
            from quill.core.scanning import NO_SCANNER_SENTENCE, ScanSession, choose_scanner

            if not scanners:
                self._scan_say_problem(NO_SCANNER_SENTENCE, parent)
                if on_cancel is not None:
                    on_cancel()
                return
            remembered = self._scan_remembered_id()
            scanner = choose_scanner(scanners, remembered)
            if scanner is None:
                scanner = self._scan_ask_which(scanners, parent=parent)
            if scanner is None:
                self._set_status("Scan cancelled")
                if on_cancel is not None:
                    on_cancel()
                return
            from quill.core.scanning.settings_fields import scan_options

            session = ScanSession(self._scan_backend(), scanner, scan_options(self.settings))
            self._scan_next_page(session, on_done, parent, on_cancel)

        self._scan_list(_listed, parent=parent, on_cancel=on_cancel)

    def _scan_list(
        self,
        on_listed: Callable[[list[Any]], None],
        *,
        parent: Any = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        backend = self._scan_backend()
        self._scan_busy = True
        self._set_status("Looking for scanners...")

        def _run(progress_callback, cancellation_token, **_kw):
            return backend.list_scanners()

        def _done(_op, scanners) -> None:
            self._scan_busy = False
            on_listed(list(scanners))

        def _failed(_op, error) -> None:
            self._scan_busy = False
            self._scan_failed(error, parent)
            if on_cancel is not None:
                on_cancel()

        self._task_manager.submit(name="scan-list", func=_run, on_success=_done, on_failure=_failed)

    def _scan_next_page(
        self,
        session: Any,
        on_done: Callable[[Any], None],
        parent: Any,
        on_cancel: Callable[[], None] | None,
    ) -> None:
        number = session.page_count + 1
        self._scan_busy = True
        self._announce(f"Scanning page {number}. This can take up to a minute.")

        def _run(progress_callback, cancellation_token, **_kw):
            return session.scan_next(cancel_requested=cancellation_token.is_cancelled)

        def _scanned(_op, _new) -> None:
            from quill.core.scanning import page_count_sentence

            self._scan_busy = False
            wx = self._wx
            count = session.page_count
            answer = self._scan_message(
                f"Page {count} scanned. Put the next page on the scanner and choose Yes "
                "to scan it, or No if you are done.\n\nScan another page?",
                wx.ICON_QUESTION | wx.YES_NO,
                parent,
                title="Scan Another Page?",
            )
            if answer == wx.YES:
                self._scan_next_page(session, on_done, parent, on_cancel)
                return
            self._set_status(f"Scanned {page_count_sentence(count)}.")
            on_done(session)

        def _failed(_op, error) -> None:
            self._scan_busy = False
            self._scan_failed(error, parent)
            if session.page_count:
                # Keep what was scanned: a jam on page three loses nothing.
                on_done(session)
                return
            session.cleanup()
            if on_cancel is not None:
                on_cancel()

        self._task_manager.submit(
            name="scan-page", func=_run, on_success=_scanned, on_failure=_failed
        )

    # ------------------------------------------------------------- pieces

    def _scan_backend(self) -> Any:
        from quill.core.scanning import backend_for_platform

        tool = str(getattr(self.settings, "scanner_tool_path", "") or "")
        return backend_for_platform(tool_path=tool)

    def _scan_remembered_id(self) -> str:
        """The remembered scanner, unless Options cleared its name."""
        settings = getattr(self, "settings", None)
        if not str(getattr(settings, "scanner_device_name", "") or "").strip():
            return ""
        return str(getattr(settings, "scanner_device", "") or "")

    def _scan_ask_which(self, scanners: list[Any], *, parent: Any = None, force: bool = False):
        """Ask which scanner (only when there are several, unless ``force``)."""
        from quill.core.scanning import scanner_labels
        from quill.ui.dialog_contract import apply_modal_ids

        if len(scanners) == 1 and not force:
            return scanners[0]
        wx = self._wx
        labels = scanner_labels(scanners)
        dialog = wx.SingleChoiceDialog(
            parent or self.frame,
            "Which scanner should QUILL use? QUILL remembers your choice.",
            "Choose Scanner",
            labels,
        )
        apply_modal_ids(dialog, affirmative_id=wx.ID_OK, escape_id=wx.ID_CANCEL)
        try:
            remembered = self._scan_remembered_id()
            index = next((i for i, s in enumerate(scanners) if s.id == remembered), 0)
            dialog.SetSelection(index)
            if self._show_modal_dialog(dialog, "Choose Scanner") != wx.ID_OK:
                return None
            scanner = scanners[int(dialog.GetSelection())]
        finally:
            dialog.Destroy()
        self._scan_remember(scanner)
        return scanner

    def _scan_remember(self, scanner: Any) -> None:
        settings = getattr(self, "settings", None)
        if settings is None:
            return
        settings.scanner_device = scanner.id
        settings.scanner_device_name = scanner.name
        try:
            from quill.core.settings import save_settings

            save_settings(settings)
        except Exception:  # noqa: BLE001 - remembering is a convenience, never a crash
            pass

    def _scan_message(self, message: str, style: int, parent: Any, *, title: str = _TITLE) -> int:
        """A message box over ``parent`` (a dialog that started the scan) or the main window."""
        if parent is None:
            return int(self._show_message_box(message, title, style))
        return int(self._show_message_box(message, title, style, parent))

    def _scan_say_problem(self, message: str, parent: Any = None) -> None:
        wx = self._wx
        self._set_status(message)
        self._scan_message(message, wx.ICON_INFORMATION | wx.OK, parent)

    def _scan_failed(self, error: BaseException, parent: Any = None) -> None:
        from quill.core.scanning.base import ScanCancelled, ScanError, plain_message

        if isinstance(error, ScanCancelled):
            self._set_status("Scan stopped.")
            return
        if isinstance(error, ScanError):
            self._scan_say_problem(plain_message(error), parent)
            return
        self._scan_say_problem(f"The scan did not work: {error}", parent)

    # -------------------------------------------- File > Import > Scan Pages's uses

    def _scan_offer_uses(self, session: Any) -> None:
        """One question: what to do with the pages."""
        from quill.core.scanning import page_count_sentence
        from quill.ui.dialog_contract import apply_modal_ids

        wx = self._wx
        uses = list(SCAN_USES)
        if session.page_count != 1 or not self._scan_can_insert():
            uses = [u for u in uses if u[0] != USE_INSERT]
        labels = [label for _key, label in uses]
        dialog = wx.SingleChoiceDialog(
            self.frame,
            f"You scanned {page_count_sentence(session.page_count)}. What would you like to do?",
            _TITLE,
            labels,
        )
        apply_modal_ids(dialog, affirmative_id=wx.ID_OK, escape_id=wx.ID_CANCEL)
        try:
            dialog.SetSelection(0)
            if self._show_modal_dialog(dialog, _TITLE) != wx.ID_OK:
                session.cleanup()
                self._set_status("The scanned pages were not kept.")
                return
            use = uses[int(dialog.GetSelection())][0]
        finally:
            dialog.Destroy()
        self._scan_use(session, use)

    def _scan_use(self, session: Any, use: str) -> None:
        from quill.core.scanning import ScanError

        try:
            if use == USE_OCR:
                # QUILL's own on-device OCR, the route Import / Convert takes
                # for a scanned PDF. The pages stay until QUILL's temporary
                # files are cleared, since the OCR review reads them.
                source = session.best_upload(self._scan_stem())
                self._docconvert_run_local_ocr(source)  # type: ignore[attr-defined]
            elif use == USE_INSERT:
                self._scan_insert(session)
            elif use == USE_PDF:
                self._scan_save_pdf(session)
            elif use == USE_IMAGES:
                self._scan_save_images(session)
        except ScanError as exc:
            from quill.core.scanning.base import plain_message

            self._scan_say_problem(plain_message(exc))
            session.cleanup()

    def _scan_stem(self) -> str:
        from quill.core.scanning.session import scan_name

        return scan_name()

    def _scan_default_folder(self) -> Path:
        from quill.core.scanning.session import documents_folder, folder_is_writable

        path = getattr(getattr(self, "document", None), "path", None)
        if path is not None and folder_is_writable(Path(path).parent):
            return Path(path).parent
        return documents_folder()

    def _scan_save_pdf(self, session: Any) -> None:
        from quill.core.scanning.session import unique_path

        wx = self._wx
        stem = self._scan_stem()
        folder = self._scan_default_folder()
        with wx.FileDialog(
            self.frame,
            "Save the scanned pages as a PDF",
            defaultDir=str(folder),
            defaultFile=unique_path(folder, stem, ".pdf").name,
            wildcard="PDF files (*.pdf)|*.pdf",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dialog:
            if self._show_modal_dialog(dialog, _TITLE) != wx.ID_OK:
                session.cleanup()
                self._set_status("The scanned pages were not saved.")
                return
            target = Path(dialog.GetPath())
        if target.suffix.lower() != ".pdf":
            target = target.with_suffix(".pdf")
        from quill.core.scanning import images_to_pdf

        images_to_pdf(session.pages, target, dpi=session.options.dpi)
        session.cleanup()
        self._announce(f"Saved {target.name} in {target.parent}.")

    def _scan_save_images(self, session: Any) -> None:
        wx = self._wx
        with wx.DirDialog(
            self.frame,
            "Choose a folder for the scanned pictures",
            defaultPath=str(self._scan_default_folder()),
            style=wx.DD_DEFAULT_STYLE,
        ) as dialog:
            if self._show_modal_dialog(dialog, _TITLE) != wx.ID_OK:
                session.cleanup()
                self._set_status("The scanned pages were not saved.")
                return
            folder = Path(dialog.GetPath())
        written = session.copy_images_to(folder, self._scan_stem())
        session.cleanup()
        what = written[0].name if len(written) == 1 else f"{len(written)} pictures"
        self._announce(f"Saved {what} in {folder}.")

    def _scan_can_insert(self) -> bool:
        document = getattr(self, "document", None)
        read_only = getattr(self, "_document_is_read_only", None)
        if document is None or (callable(read_only) and read_only()):
            return False
        return callable(getattr(self, "insert_image", None))

    def _scan_insert(self, session: Any) -> None:
        """Keep the page beside the document (or in Documents), then Insert Image."""
        from quill.core.scanning.session import unique_path

        page = session.pages[0]
        folder = self._scan_default_folder()
        target = unique_path(folder, self._scan_stem(), page.suffix or ".png")
        import shutil

        try:
            shutil.copyfile(page, target)
        except OSError as exc:
            self._scan_say_problem(f"QUILL could not keep the scanned page: {exc.strerror or exc}")
            return
        finally:
            session.cleanup()
        self._set_status(f"The scanned page is saved as {target.name} in {target.parent}.")
        self.insert_image(initial_path=str(target))  # type: ignore[attr-defined]


__all__ = ["SCAN_USES", "ScanningMixin"]
