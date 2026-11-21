"""Convert with Scribe for Documents (MainFrame mixin).

Three commands in Tools > Reading and Dictation > OCR and Document
Conversion, beside the Datalab cloud tier they resemble:

* **Convert with Scribe for Documents...** -- choose a file, the current
  document or a web address, choose what to make, agree to the upload, and
  hear every status update Scribe sends. HTML (the default) opens as a new
  QUILL document for editing; every other format is saved to a folder.
* **Sign In to Scribe for Documents...** -- Scribe's own sign-in page in the
  system browser (OAuth 2.0 + PKCE); the tokens go to the secret vault.
* **Sign Out of Scribe for Documents** -- forgets them.

Every network step runs on the background task pool through
:mod:`quill.core.scribe_documents` and :mod:`quill.core.scribe_signin`; this
module only asks questions and reports. One document at a time: a second
Convert while one is running is refused with a sentence.

Wiring expectations from MainFrame: ``_wx``, ``frame``, ``document``,
``_task_manager``, ``_show_modal_dialog``, ``_show_message_box``,
``_create_document_tab``, ``_set_tab_page_text``, ``_set_status``,
``_announce``.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quill.core.document import Document
from quill.core.import_convert import converted_name

_TITLE = "Convert with Scribe for Documents"
_SIGN_IN_TITLE = "Sign In to Scribe for Documents"
_SOURCES = (
    "A file on this computer",
    "The current document",
    "A web page or online file (URL)",
)


def scribe_wildcard() -> str:
    from quill.core.scribe_documents import SCRIBE_INPUT_EXTENSIONS

    mask = ";".join(f"*{ext}" for ext in sorted(SCRIBE_INPUT_EXTENSIONS))
    return f"Files Scribe can convert ({mask})|{mask}|All files (*.*)|*.*"


def stem_for_url(url: str) -> str:
    """A file-name stem for a web address: its last path part, or its host."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    last = Path(parts.path).stem or parts.hostname or "web page"
    return re.sub(r"[^\w .-]+", "_", last).strip(" ._") or "web page"


def unique_path(folder: Path, stem: str, extension: str) -> Path:
    candidate = folder / f"{stem}{extension}"
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({number}){extension}"
        number += 1
    return candidate


def html_bytes_to_text(data: bytes, stem: str) -> tuple[str, tuple[str, ...]]:
    """Scribe's HTML result as QUILL Markdown, through the import readers.

    The HTML may arrive as one page or zipped with its images; only the page
    is read. Returns the text and any reader notes.
    """
    from quill.io.docconvert import convert_born_digital

    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(_bytes_io(data)) as archive:
            pages = [n for n in archive.namelist() if n.lower().endswith((".html", ".htm"))]
            if not pages:
                return "", ("The HTML result held no page.",)
            data = archive.read(sorted(pages, key=len)[0])
    with tempfile.TemporaryDirectory(prefix="quill-scribe-") as tmp:
        page = Path(tmp) / f"{stem or 'scribe'}.html"
        page.write_bytes(data)
        outcome = convert_born_digital(page)
    return outcome.text, tuple(str(note) for note in outcome.warnings)


def _bytes_io(data: bytes) -> Any:
    import io

    return io.BytesIO(data)


class ScribeDocumentsMixin:
    """``tools.scribe_convert``, ``tools.scribe_sign_in``, ``tools.scribe_sign_out``."""

    _scribe_busy: bool = False

    # ------------------------------------------------------------- wiring

    def _register_scribe_commands(self) -> None:
        """The three commands, none with a default key: once-in-a-while cloud
        commands like the other OCR services beside them."""
        host: Any = self
        host.commands.register(
            "tools.scribe_convert",
            "Convert with Scribe for Documents",
            self.convert_with_scribe,
            host._binding_for("tools.scribe_convert"),
        )
        host.commands.register(
            "tools.scribe_sign_in",
            "Sign In to Scribe for Documents",
            self.scribe_sign_in,
            host._binding_for("tools.scribe_sign_in"),
        )
        host.commands.register(
            "tools.scribe_sign_out",
            "Sign Out of Scribe for Documents",
            self.scribe_sign_out,
            host._binding_for("tools.scribe_sign_out"),
        )

    def _append_scribe_rows(self, conversion_menu: Any) -> None:
        """Tools > Reading and Dictation > OCR and Document Conversion rows."""
        from quill.core.i18n import _

        wx = self._wx
        host: Any = self
        self._id_scribe_convert = wx.NewIdRef()
        self._id_scribe_sign_in = wx.NewIdRef()
        self._id_scribe_sign_out = wx.NewIdRef()
        conversion_menu.Append(
            self._id_scribe_convert,
            host._menu_label(_("&Convert with Scribe for Documents..."), "tools.scribe_convert"),
        )
        conversion_menu.Append(
            self._id_scribe_sign_in,
            host._menu_label(_("Si&gn In to Scribe for Documents..."), "tools.scribe_sign_in"),
        )
        conversion_menu.Append(
            self._id_scribe_sign_out,
            host._menu_label(_("Sign &Out of Scribe for Documents"), "tools.scribe_sign_out"),
        )
        host.frame.Bind(
            wx.EVT_MENU, lambda _e: self.convert_with_scribe(), id=self._id_scribe_convert
        )
        host.frame.Bind(wx.EVT_MENU, lambda _e: self.scribe_sign_in(), id=self._id_scribe_sign_in)
        host.frame.Bind(wx.EVT_MENU, lambda _e: self.scribe_sign_out(), id=self._id_scribe_sign_out)

    # ------------------------------------------------------------ commands

    def convert_with_scribe(self) -> None:
        """Choose a source and a format, ask consent, and convert."""
        from quill.core import scribe_documents as scribe

        if not self._scribe_ready():
            return
        if self._scribe_busy:
            self._set_status(
                "Scribe for Documents is already converting a document. It takes one at a time."
            )
            return
        if not scribe.signed_in():
            wx = self._wx
            choice = self._show_message_box(
                "You are not signed in to Scribe for Documents. Sign in now? "
                "Scribe's sign-in page opens in your web browser.",
                _TITLE,
                wx.ICON_QUESTION | wx.YES_NO,
            )
            if choice == wx.YES:
                self.scribe_sign_in(then=self.convert_with_scribe)
            else:
                self._set_status("Convert with Scribe for Documents cancelled")
            return
        job = self._scribe_choose_job()
        if job is None:
            self._set_status("Convert with Scribe for Documents cancelled")
            return
        job, folder, temp_dir = job
        if not self._scribe_consent(job):
            self._scribe_cleanup(temp_dir)
            self._set_status("Scribe conversion cancelled; nothing was uploaded")
            return
        self._scribe_submit(job, folder, temp_dir)

    def scribe_sign_in(self, then: Callable[[], None] | None = None) -> None:
        """Open Scribe's sign-in page and wait for it on a worker thread."""
        from quill.core.scribe_signin import sign_in

        if not self._scribe_ready():
            return
        self._set_status(
            "Opening the Scribe for Documents sign-in page in your browser. "
            "Sign in there, then come back to QUILL."
        )

        def _run(progress_callback, cancellation_token, **_kw):
            return sign_in(cancel_requested=cancellation_token.is_cancelled)

        def _done(_op, _tokens) -> None:
            self._announce("Signed in to Scribe for Documents.")
            if then is not None:
                then()

        self._task_manager.submit(
            name="scribe-sign-in",
            func=_run,
            on_success=_done,
            on_failure=lambda _op, error: self._scribe_failed(error, _SIGN_IN_TITLE),
        )

    def scribe_sign_out(self) -> None:
        from quill.core.scribe_documents import sign_out

        if sign_out():
            self._announce("Signed out of Scribe for Documents.")
        else:
            self._set_status("You were not signed in to Scribe for Documents.")

    # ------------------------------------------------------------ choosing

    def _scribe_ready(self) -> bool:
        from quill.core import scribe_documents as scribe

        if scribe.safe_mode_active() or getattr(self, "_safe_mode", False):
            self._set_status("Scribe for Documents is off in Safe Mode.")
            return False
        if not scribe.client_id_configured():
            wx = self._wx
            self._show_message_box(
                "This copy of QUILL is not registered with Scribe for Documents yet, "
                "so it cannot sign in. A release build will include the "
                f"registration. Developers can set {scribe.SCRIBE_CLIENT_ID_ENV}.",
                _TITLE,
                wx.ICON_INFORMATION | wx.OK,
            )
            return False
        return True

    def _scribe_choice(self, prompt: str, labels: list[str], selection: int = 0) -> int:
        """A native single-choice list; returns the index or -1 on Cancel."""
        from quill.ui.dialog_contract import apply_modal_ids

        wx = self._wx
        dialog = wx.SingleChoiceDialog(self.frame, prompt, _TITLE, labels)
        apply_modal_ids(dialog, affirmative_id=wx.ID_OK, escape_id=wx.ID_CANCEL)
        try:
            dialog.SetSelection(selection)
            if self._show_modal_dialog(dialog, _TITLE) != wx.ID_OK:
                return -1
            return int(dialog.GetSelection())
        finally:
            dialog.Destroy()

    def _scribe_choose_job(self) -> tuple[Any, Path | None, Path | None] | None:
        """Source, then format, then (for a saved format) the folder."""
        from quill.core import scribe_documents as scribe

        source = self._scribe_choice("What do you want to convert?", list(_SOURCES))
        temp_dir: Path | None = None
        if source == 0:
            path = self._scribe_pick_file()
            if path is None:
                return None
            job = scribe.ScribeJob(path=path)
        elif source == 1:
            picked = self._scribe_current_document()
            if picked is None:
                return None
            job, temp_dir = picked
        elif source == 2:
            url = self._scribe_ask_url()
            if url is None:
                return None
            job = scribe.ScribeJob(url=url, display_name=url)
        else:
            return None
        labels = [item.label for item in scribe.OUTPUT_FORMATS]
        picked_format = self._scribe_choice(
            f"What should Scribe make from {job.name}? {scribe.POWERED_BY}.", labels
        )
        if picked_format < 0:
            self._scribe_cleanup(temp_dir)
            return None
        fmt = scribe.OUTPUT_FORMATS[picked_format]
        job.output_format = fmt.id
        folder: Path | None = None
        if not fmt.opens_in_quill:
            folder = self._scribe_pick_folder(fmt.label)
            if folder is None:
                self._scribe_cleanup(temp_dir)
                return None
        return job, folder, temp_dir

    def _scribe_pick_file(self) -> Path | None:
        wx = self._wx
        dialog = wx.FileDialog(
            self.frame,
            "Choose a file to convert with Scribe for Documents",
            wildcard=scribe_wildcard(),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if self._show_modal_dialog(dialog, _TITLE) != wx.ID_OK:
                return None
            return Path(dialog.GetPath())
        finally:
            dialog.Destroy()

    def _scribe_pick_folder(self, label: str) -> Path | None:
        wx = self._wx
        path = getattr(getattr(self, "document", None), "path", None)
        with wx.DirDialog(
            self.frame,
            f"Choose a folder for the {label}",
            defaultPath=str(Path(path).parent) if path else "",
            style=wx.DD_DEFAULT_STYLE,
        ) as dialog:
            if self._show_modal_dialog(dialog, _TITLE) != wx.ID_OK:
                return None
            return Path(dialog.GetPath())

    def _scribe_ask_url(self) -> str | None:
        wx = self._wx
        with wx.TextEntryDialog(
            self.frame,
            "Web address of the page or file for Scribe for Documents to convert:",
            _TITLE,
        ) as dialog:
            dialog.SetHelpText(
                "Type or paste a full address starting with https://. Scribe fetches "
                "it itself; QUILL uploads nothing from this computer."
            )
            if self._show_modal_dialog(dialog, _TITLE) != wx.ID_OK:
                return None
            value = str(dialog.GetValue()).strip()
        return value or None

    def _scribe_current_document(self) -> tuple[Any, Path | None] | None:
        """The front document: its saved file when Scribe can read it as is,
        otherwise a Word copy of the text (headings and lists survive)."""
        from quill.core import scribe_documents as scribe

        document = getattr(self, "document", None)
        text = str(getattr(document, "text", "") or "")
        if not text.strip():
            self._set_status("The current document is empty; there is nothing to convert.")
            return None
        path = getattr(document, "path", None)
        modified = bool(getattr(document, "modified", False))
        if path is not None and not modified:
            path = Path(path)
            if path.suffix.lower() in scribe.SCRIBE_INPUT_EXTENSIONS and path.is_file():
                return scribe.ScribeJob(path=path), None
        stem = Path(path).stem if path is not None else "Untitled"
        temp_dir = Path(tempfile.mkdtemp(prefix="quill-scribe-"))
        target = temp_dir / f"{stem}.docx"
        try:
            from quill.io.docx_writer import write_docx

            write_docx(Document(text=text, path=None), target)
        except Exception:  # noqa: BLE001 - python-docx missing: plain text still works
            target = temp_dir / f"{stem}.txt"
            target.write_text(text, encoding="utf-8")
        return scribe.ScribeJob(path=target, display_name=f"{stem} (current document)"), temp_dir

    def _scribe_consent(self, job: Any) -> bool:
        """The per-use consent, every time, naming what is sent and where."""
        from quill.core.datalab_ocr import looks_sensitive

        wx = self._wx
        if job.path is not None:
            what = f"QUILL will upload {job.name} to Scribe for Documents (scribeit.io)"
        else:
            what = f"Scribe for Documents (scribeit.io) will fetch {job.url}"
        message = (
            f"{what} and convert it with your Scribe account. The conversion uses "
            "your Scribe page credits or subscription. The document and its "
            "conversions are kept in your Scribe library, where you can delete them.\n\n"
            "Only continue if you are allowed to share this document with that "
            "service. Consider whether it contains private, medical, legal, "
            "educational, financial or confidential information.\n\n"
            "Powered by Scribe for Documents. Convert it now?"
        )
        if job.path is not None and looks_sensitive(job.path):
            message = (
                "CAUTION: this file's name suggests it may contain sensitive "
                "personal information.\n\n" + message
            )
        return self._show_message_box(message, "Upload Consent", wx.ICON_WARNING | wx.YES_NO) == (
            wx.YES
        )

    def _scribe_ask_password(self, name: str, wrong: bool) -> str | None:
        from quill.ui.dialog_contract import apply_modal_ids

        wx = self._wx
        prompt = (
            f"That password did not unlock {name}. Type the password again:"
            if wrong
            else f"{name} is protected. Type its password:"
        )
        with wx.PasswordEntryDialog(self.frame, prompt, "Document Password") as dialog:
            dialog.SetHelpText(
                "QUILL sends this password to Scribe for Documents with the "
                "conversion, and Scribe keeps it with the document in your library "
                "so later conversions do not ask again. QUILL does not save it."
            )
            apply_modal_ids(dialog, affirmative_id=wx.ID_OK, cancel_id=wx.ID_CANCEL)
            if self._show_modal_dialog(dialog, "Document Password") != wx.ID_OK:
                return None
            value = str(dialog.GetValue())
        return value or None

    # ------------------------------------------------------------- running

    def _scribe_submit(self, job: Any, folder: Path | None, temp_dir: Path | None) -> None:
        from quill.core import scribe_documents as scribe

        fmt = scribe.format_by_id(job.output_format)
        stem = job.path.stem if job.path is not None else stem_for_url(job.url)
        if job.display_name.endswith("(current document)"):
            stem = job.display_name.removesuffix(" (current document)")
        self._scribe_busy = True
        self._set_status(f"Starting the conversion of {job.name}. {scribe.POWERED_BY}.")

        def _run(progress_callback, cancellation_token, **_kw):
            result = scribe.run_conversion(
                job,
                on_status=progress_callback,
                cancel_requested=cancellation_token.is_cancelled,
            )
            if result.kind != scribe.DONE or not fmt.opens_in_quill:
                return result, None
            return result, html_bytes_to_text(result.data, stem)

        def _done(_op, payload) -> None:
            result, converted = payload
            if result.kind in (scribe.PASSWORD_NEEDED, scribe.PASSWORD_WRONG):
                self._scribe_busy = False
                password = self._scribe_ask_password(
                    job.name, wrong=result.kind == scribe.PASSWORD_WRONG
                )
                if password is None:
                    self._scribe_cleanup(temp_dir)
                    self._set_status(f"{job.name} stays locked; the conversion stopped.")
                    return
                job.password = password
                self._scribe_submit(job, folder, temp_dir)
                return
            self._scribe_finish(job, fmt, result, converted, folder, stem)
            self._scribe_cleanup(temp_dir)

        def _failed(_op, error) -> None:
            self._scribe_cleanup(temp_dir)
            self._scribe_failed(error, _TITLE)

        self._task_manager.submit(
            name="scribe-convert",
            func=_run,
            on_success=_done,
            on_failure=_failed,
            on_progress=lambda _op, message: self._set_status(str(message)),
        )

    def _scribe_finish(
        self,
        job: Any,
        fmt: Any,
        result: Any,
        converted: tuple[str, tuple[str, ...]] | None,
        folder: Path | None,
        stem: str,
    ) -> None:
        from quill.core import scribe_documents as scribe
        from quill.core.storage import write_bytes_atomic

        self._scribe_busy = False
        if converted is not None:
            text, notes = converted
            index = self._create_document_tab(
                Document(text=text, path=None, modified=True), select=True
            )
            self._set_tab_page_text(index, converted_name(stem))
            self._announce(
                f"Scribe for Documents converted {job.name}. It is open as a new, unsaved document."
            )
            for note in notes:
                self._set_status(note)
            return
        target = unique_path(folder or Path.home(), stem, scribe.output_extension(fmt, result.data))
        try:
            write_bytes_atomic(target, result.data)
        except OSError as exc:
            self._scribe_failed(
                scribe.ScribeDocumentsError(
                    f"QUILL could not save {target.name}: {exc.strerror or exc}."
                ),
                _TITLE,
            )
            return
        self._announce(f"Scribe for Documents saved {target.name} in {target.parent}.")

    def _scribe_failed(self, error: BaseException, title: str) -> None:
        from quill.core import scribe_documents as scribe

        self._scribe_busy = False
        wx = self._wx
        if isinstance(error, scribe.ScribeCancelled):
            self._set_status(str(error))
            return
        if isinstance(error, scribe.ScribeSignInRequired):
            choice = self._show_message_box(
                f"{error}\n\nSign in to Scribe for Documents now?",
                title,
                wx.ICON_INFORMATION | wx.YES_NO,
            )
            if choice == wx.YES:
                self.scribe_sign_in()
            return
        self._set_status(
            "Scribe for Documents conversion failed"
            if title == _TITLE
            else "Sign-in to Scribe for Documents failed"
        )
        self._show_message_box(str(error), title, wx.ICON_ERROR | wx.OK)

    @staticmethod
    def _scribe_cleanup(temp_dir: Path | None) -> None:
        if temp_dir is None:
            return
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)
