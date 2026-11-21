"""Scribe for Documents in QUILL: commands, menus and entry points (Scribe PRD 8.1).

Tools > Scribe for Documents holds, in this order (8.1.8): Convert a File...,
Convert This Document..., Convert from a Link..., My Scribe Documents,
Conversions, Sign In, Sign Out, Open My Scribe Account in Browser, Options,
About Scribe for Documents. Also: File > My Scribe Documents (7.5), File >
Import > Convert with Scribe for Documents... (the Import tier, 8.1.1),
File > Export > With Scribe for Documents... (8.1.4), Braille > Translate
with Scribe for Documents... (8.1.5), Window > Scribe Conversions (7.4).

Every entry point ends in :meth:`ScribeDocumentsMixin._scribe_convert`: Safe
Mode off, signed in (the sign-in dialog's Continue carries on into the
command), the one Convert dialog, then the family queue. The queue, the
speaking and the results are ``main_frame_scribe_queue.py``.

Command ids are ``scribe.*``. Keys are deliberately few: Convert a File,
Conversions, Scribe Status and Repeat Last Scribe Message (all on the QUILL
key); the rest are reached from these menus (the family gate's ``scribe.``
row says why).

Wiring expectations from MainFrame: ``_wx``, ``frame``, ``document``,
``settings``, ``commands``, ``_binding_for``, ``_menu_label``,
``_task_manager``, ``_show_modal_dialog``, ``_show_message_box``,
``_set_status``, ``_announce``, ``open_file``, ``_reveal_in_explorer``,
``open_general_preferences``.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quill.core.document import Document

APP_NAME = "QUILL"
TITLE = "Scribe for Documents"

#: (command id, menu label, handler name). The order of Tools > Scribe for Documents.
TOOLS_ROWS: tuple[tuple[str, str, str], ...] = (
    ("scribe.convert_file", "&Convert a File...", "scribe_convert_file"),
    ("scribe.convert_document", "Convert &This Document...", "scribe_convert_document"),
    ("scribe.convert_link", "Convert from a &Link...", "scribe_convert_link"),
    ("scribe.my_documents", "&My Scribe Documents", "scribe_my_documents"),
    ("scribe.conversions", "Con&versions", "scribe_conversions"),
    ("scribe.sign_in", "&Sign In...", "scribe_sign_in"),
    ("scribe.sign_out", "Sign &Out", "scribe_sign_out"),
    ("scribe.account", "Open My Scribe &Account in Browser", "scribe_open_account"),
    ("scribe.options", "O&ptions...", "scribe_options"),
    ("scribe.about", "A&bout Scribe for Documents", "scribe_about"),
)
#: Commands outside that menu: (id, palette title, handler name).
OTHER_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("scribe.import", "Import / Convert with Scribe for Documents...", "scribe_import"),
    ("scribe.export", "Export with Scribe for Documents...", "scribe_export"),
    ("scribe.braille", "Translate with Scribe for Documents...", "scribe_braille"),
    ("scribe.status", "Scribe Status", "scribe_status"),
    ("scribe.repeat", "Repeat Last Scribe Message", "scribe_repeat_last"),
)


def scribe_wildcard() -> str:
    from quill.core.scribe.consent import INPUT_EXTENSIONS

    mask = ";".join(f"*{ext}" for ext in sorted(INPUT_EXTENSIONS))
    return f"Files Scribe can convert ({mask})|{mask}|All files (*.*)|*.*"


def palette_title(command_id: str, label: str) -> str:
    """The Command Palette's name: every Scribe command names the service (SCR-POL-1)."""
    title = label.replace("&", "")
    if "Scribe" not in title:
        title = (
            f"{title.rstrip('.')} (Scribe for Documents){'...' if title.endswith('...') else ''}"
        )
    return title


class ScribeDocumentsMixin:
    """The ``scribe.*`` commands. The queue half is ``ScribeQueueMixin``."""

    # ------------------------------------------------------------ wiring

    def _register_scribe_commands(self) -> None:
        host: Any = self
        for command_id, label, handler in (*TOOLS_ROWS, *OTHER_COMMANDS):
            host.commands.register(
                command_id,
                palette_title(command_id, label),
                getattr(self, handler),
                host._binding_for(command_id),
            )
        call_later = getattr(self._wx, "CallLater", None)
        if callable(call_later):
            call_later(3000, self._scribe_resume_on_startup)  # type: ignore[attr-defined]

    def _append_scribe_rows(self, tools_menu: Any) -> None:
        """Tools > Scribe for Documents (PRD 8.1.8)."""
        from quill.core.i18n import _

        wx = self._wx
        host: Any = self
        menu = wx.Menu()
        for command_id, label, handler in TOOLS_ROWS:
            item_id = wx.NewIdRef()
            menu.Append(item_id, host._menu_label(_(label), command_id))
            action = getattr(self, handler)
            host.frame.Bind(wx.EVT_MENU, lambda _e, run=action: run(), id=item_id)
            if command_id in ("scribe.convert_link", "scribe.conversions", "scribe.account"):
                menu.AppendSeparator()
        tools_menu.AppendSubMenu(menu, _("Scribe &for Documents"))
        self._append_choose_scanner_row(tools_menu)  # type: ignore[attr-defined]

    def _append_scribe_file_rows(self, file_menu: Any, import_menu: Any, export_menu: Any) -> None:
        """File > My Scribe Documents, and the Import and Export rows."""
        from quill.core.i18n import _

        wx = self._wx
        host: Any = self
        rows = (
            (
                import_menu,
                "scribe.import",
                "Convert with &Scribe for Documents...",
                self.scribe_import,
            ),
            (
                export_menu,
                "scribe.export",
                "With &Scribe for Documents (tagged PDF, MOBI, DAISY with audio, large print)...",
                self.scribe_export,
            ),
            (file_menu, "scribe.my_documents", "My Scribe Doc&uments", self.scribe_my_documents),
        )
        for menu, command_id, label, handler in rows:
            if menu is not file_menu:
                menu.AppendSeparator()
            item_id = wx.NewIdRef()
            menu.Append(item_id, host._menu_label(_(label), command_id))
            host.frame.Bind(wx.EVT_MENU, lambda _e, run=handler: run(), id=item_id)

    def _append_scribe_window_rows(self, window_menu: Any) -> None:
        from quill.core.i18n import _

        wx = self._wx
        host: Any = self
        item_id = wx.NewIdRef()
        window_menu.Append(
            item_id, host._menu_label(_("Scri&be Conversions"), "scribe.conversions")
        )
        host.frame.Bind(wx.EVT_MENU, lambda _e: self.scribe_conversions(), id=item_id)

    def _append_scribe_braille_row(self, braille_menu: Any) -> None:
        from quill.core.i18n import _

        wx = self._wx
        host: Any = self
        item_id = wx.NewIdRef()
        label = _("Translate with &Scribe for Documents...")
        braille_menu.Append(item_id, host._menu_label(label, "scribe.braille"))
        host.frame.Bind(wx.EVT_MENU, lambda _e: self.scribe_braille(), id=item_id)

    # ------------------------------------------------------------ commands

    def scribe_convert_file(self, *, formats: str = "", purpose: str = "") -> None:
        """Convert a File...: one picker (several files allowed), then the dialog."""
        if not self._scribe_allowed():
            return
        paths = self._scribe_pick_files()
        if paths:
            sources = [self._scribe_source(p) for p in paths]
            self._scribe_convert(sources, formats=formats, purpose=purpose)

    def scribe_import(self) -> None:
        """File > Import > Convert with Scribe for Documents: Word, as a new tab (8.1.1)."""
        self.scribe_convert_file(formats="docx", purpose="import")

    def scribe_convert_document(self) -> None:
        """Convert This Document...: the file itself when Scribe reads it, else a Word copy."""
        if not self._scribe_allowed():
            return
        source = self._scribe_this_document()
        if source is not None:
            self._scribe_convert([source])

    def scribe_export(self) -> None:
        """File > Export > With Scribe for Documents: saved first as Word (8.1.4)."""
        if not self._scribe_allowed():
            return
        source = self._scribe_this_document(always_docx=True)
        if source is not None:
            self._scribe_convert([source], formats="pdf")

    def scribe_braille(self) -> None:
        """Braille > Translate with Scribe for Documents: BRF, opened in the braille view."""
        if not self._scribe_allowed():
            return
        source = self._scribe_this_document(always_docx=True)
        if source is not None:
            self._scribe_convert([source], formats="brf", purpose="braille")

    def scribe_convert_link(self) -> None:
        if not self._scribe_allowed():
            return
        from quill.core.scribe.consent import SourceInfo

        wx = self._wx
        title = "Convert from a Link"
        with wx.TextEntryDialog(self.frame, "Link to the document:", title) as dialog:
            dialog.SetHelpText(
                "A link that points straight at a file, such as a PDF. Web pages and "
                "links behind a sign-in are not supported. Scribe fetches it itself."
            )
            if self._show_modal_dialog(dialog, title) != wx.ID_OK:
                return
            url = str(dialog.GetValue()).strip()
        from quill.core.scribe.client import check_link
        from quill.core.scribe.model import ScribeError

        try:
            url = check_link(url)
        except ScribeError as exc:
            self._show_message_box(exc.message, TITLE, wx.ICON_INFORMATION | wx.OK)
            return
        self._scribe_convert([SourceInfo(url=url)])

    def scribe_my_documents(self) -> None:
        if not self._scribe_allowed() or not self._scribe_signed_in(self.scribe_my_documents):
            return
        from quill.ui.scribe_documents_dialog import show

        show(self)

    def scribe_conversions(self) -> None:
        from quill.ui.scribe_conversions_window import open_window

        open_window(self)

    def scribe_sign_in(self, then: Callable[[], None] | None = None) -> bool:
        if not self._scribe_allowed():
            return False
        from quill.ui.scribe_signin_dialog import sign_in

        done = sign_in(self, app=APP_NAME)
        if done:
            # Jobs paused for the sign-in carry on (PRD 11; independent review Q4).
            self.scribe_resume_after_sign_in()  # type: ignore[attr-defined]
            if then is not None:
                then()
        return done

    def scribe_sign_out(self) -> None:
        from quill.core.scribe import queue as q
        from quill.core.scribe.auth import sign_out

        wx = self._wx
        waiting = q.JobQueue().waiting_count()
        if waiting:
            count = (
                "One conversion is" if waiting == 1 else f"{_count_word(waiting)} conversions are"
            )
            answer = self._show_message_box(
                f"{count} waiting. Signing out stops {APP_NAME} waiting for them; they "
                "still finish in your Scribe account. Sign out?",
                "Sign Out of Scribe for Documents",
                wx.ICON_QUESTION | wx.YES_NO,
            )
            if answer != wx.YES:
                return
        self._scribe_in_background(
            sign_out,
            lambda was: self._announce(
                "Signed out of Scribe for Documents."
                if was
                else "You were not signed in to Scribe for Documents."
            ),
        )

    def scribe_open_account(self) -> None:
        if not self._scribe_allowed() or not self._scribe_signed_in(self.scribe_open_account):
            return
        import webbrowser

        from quill.core.scribe.client import ScribeClient

        self._scribe_in_background(lambda: ScribeClient().account_page_url(), webbrowser.open)

    def scribe_options(self) -> None:
        self.open_general_preferences(focus_key="scribe_open_results")  # type: ignore[attr-defined]

    # ------------------------------------------------------------ the one path

    def _scribe_allowed(self) -> bool:
        """Safe Mode (and an unregistered build) turn every command off (QUILL-SCRIBE-OFF)."""
        from quill.core.scribe.auth import check_allowed
        from quill.core.scribe.model import ScribeError, plain

        try:
            check_allowed()
        except ScribeError as exc:
            self._set_status(plain(exc))
            return False
        return True

    def _scribe_signed_in(self, then: Callable[[], None]) -> bool:
        """Signed in, or the sign-in dialog now; its Continue carries on into ``then``."""
        from quill.core.scribe.auth import TokenStore

        if TokenStore().signed_in():
            return True
        self.scribe_sign_in(then=then)
        return False

    def _scribe_convert(
        self,
        sources: list[Any],
        *,
        formats: str = "",
        purpose: str = "",
        reuse: Any = None,
    ) -> None:
        """The Convert dialog for ``sources``, then the family queue."""
        from quill.core.scribe.model import DocumentSettings
        from quill.core.scribe.settings_fields import chosen_formats
        from quill.ui.scribe_convert_dialog import ScribeConvertDialog

        def _again() -> None:
            self._scribe_convert(sources, formats=formats, purpose=purpose, reuse=reuse)

        if not self._scribe_signed_in(_again):
            return
        settings = getattr(self, "settings", None)
        start = [formats] if formats else chosen_formats(settings)
        local = DocumentSettings.from_json(getattr(settings, "scribe_local_settings", {}) or {})
        told = bool(getattr(settings, "scribe_privacy_told", False))
        dialog = ScribeConvertDialog(
            self,
            sources,
            formats=start,
            settings=local,
            first_upload=not told and reuse is None,
            reuse_title=getattr(reuse, "title", "") if reuse is not None else "",
            ready_formats=tuple(reuse.ready_formats()) if reuse is not None else (),
            ai_off=self._scribe_ai_off(),
        )
        try:
            request = dialog.show()
        finally:
            dialog.close()
        if request is None:
            self._set_status("Nothing was sent to Scribe for Documents.")
        else:
            self._scribe_queue_request(sources, request, purpose=purpose, reuse=reuse)

    def _scribe_queue_request(
        self, sources: list[Any], request: Any, *, purpose: str, reuse: Any
    ) -> None:
        """Put what the person agreed to into the family queue."""
        from quill.core.scribe import queue as q
        from quill.core.scribe.library import download_folder

        settings = getattr(self, "settings", None)
        root = str(getattr(settings, "scribe_download_folder", "") or "")
        jobs = []
        for source in sources:
            title = reuse.title if reuse is not None else source.name
            jobs.append(
                q.Job(
                    app=APP_NAME,
                    title=title,
                    source_path=str(source.path) if source.path is not None else "",
                    source_url=source.url,
                    document_id=reuse.id if reuse is not None else "",
                    formats=list(request.formats),
                    settings=request.settings.to_json(),
                    save_account_defaults=request.save_account_defaults,
                    folder=str(download_folder(title, root)),
                    purpose=purpose,
                )
            )
        batch = bool(getattr(settings, "scribe_batch_exception", False))
        try:
            q.JobQueue().add(jobs, batch_allowed=batch)
        except q.QueueUnreadable as exc:
            self._announce(f"Nothing was sent. {exc.message}")
            return
        for job in jobs:
            password = (
                request.passwords.get(Path(job.source_path).name) if job.source_path else None
            )
            if password:
                self.scribe_give_password(job.id, password)  # type: ignore[attr-defined]
        self._scribe_remember(request)
        self.scribe_queue_wake()  # type: ignore[attr-defined]
        if len(jobs) > 1 and not batch:
            self._set_status(
                f"{len(jobs)} files are in Scribe Conversions. Each next file waits for "
                "Send Next File."
            )

    def _scribe_remember(self, request: Any) -> None:
        settings = getattr(self, "settings", None)
        if settings is None:
            return
        settings.scribe_last_formats = ",".join(request.formats)
        stored = request.settings.stored()
        if self._scribe_ai_off() and not stored.get("add_image_descriptions"):
            # The dialog started it off only because AI is off in QUILL; that
            # is not the person's choice, so their own default is kept (Q16).
            previous = getattr(settings, "scribe_local_settings", {}) or {}
            stored["add_image_descriptions"] = bool(previous.get("add_image_descriptions", True))
        settings.scribe_local_settings = stored
        settings.scribe_privacy_told = True
        try:
            from quill.core.settings import save_settings

            save_settings(settings)
        except Exception:  # noqa: BLE001 - remembering is a convenience
            pass

    def scribe_convert_existing(self, document: Any) -> None:
        """My Scribe Documents > Download Formats: the dialog without the upload."""
        from quill.core.scribe.consent import SourceInfo

        self._scribe_convert([SourceInfo(url="")], reuse=document)

    def scribe_open_existing(self, document: Any) -> None:
        """My Scribe Documents > Open in QUILL: the Word version, opened here."""
        from quill.core.scribe import queue as q
        from quill.core.scribe.library import Library, download_folder

        for path in Library().files_for(document.id):
            if path.suffix.lower() in (".docx", ".html", ".htm"):
                self.scribe_open_result(path)  # type: ignore[attr-defined]
                return
        root = str(getattr(getattr(self, "settings", None), "scribe_download_folder", "") or "")
        job = q.Job(
            app=APP_NAME,
            title=document.title,
            document_id=document.id,
            formats=["docx"],
            folder=str(download_folder(document.title, root)),
            purpose="open",
        )
        q.JobQueue().add([job])
        self.scribe_queue_wake()  # type: ignore[attr-defined]

    # ------------------------------------------------------------ pieces

    def _scribe_pick_files(self) -> list[Path]:
        wx = self._wx
        dialog = wx.FileDialog(
            self.frame,
            "Choose a file to convert with Scribe for Documents",
            wildcard=scribe_wildcard(),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
        )
        try:
            if self._show_modal_dialog(dialog, TITLE) != wx.ID_OK:
                return []
            return [Path(p) for p in dialog.GetPaths()]
        finally:
            dialog.Destroy()

    @staticmethod
    def _scribe_source(path: Path) -> Any:
        from quill.core.scribe.consent import describe_source

        return describe_source(path)

    def _scribe_this_document(self, *, always_docx: bool = False) -> Any:
        """The front document: its saved file when Scribe reads it as is, otherwise
        a Word copy (the richest input QUILL writes) in a private temp folder."""
        from quill.core.scribe.consent import can_convert

        document = getattr(self, "document", None)
        text = str(getattr(document, "text", "") or "")
        if not text.strip():
            self._set_status("The document is empty; there is nothing to convert.")
            return None
        path = getattr(document, "path", None)
        modified = bool(getattr(document, "modified", False))
        if not always_docx and path is not None and not modified and can_convert(Path(path)):
            if Path(path).is_file():
                return self._scribe_source(Path(path))
        stem = Path(path).stem if path is not None else "Untitled"
        folder = Path(tempfile.mkdtemp(prefix="quill-scribe-"))
        target = folder / f"{stem}.docx"
        try:
            from quill.io.docx_writer import write_docx

            write_docx(Document(text=text, path=None), target)
        except Exception:  # noqa: BLE001 - python-docx missing: plain text still converts
            target = folder / f"{stem}.txt"
            target.write_text(text, encoding="utf-8")
        return self._scribe_source(target)

    def _scribe_ai_off(self) -> bool:
        try:
            from quill.core.ai.model_manager import load_ai_enabled

            return not load_ai_enabled()
        except Exception:  # noqa: BLE001
            return False

    def _scribe_in_background(self, work: Callable[[], Any], then: Callable[[Any], Any]) -> None:
        from quill.core.scribe.model import plain

        self._task_manager.submit(
            name="scribe-command",
            func=lambda progress_callback, cancellation_token, **_kw: work(),
            on_success=lambda _op, result: then(result),
            on_failure=lambda _op, error: self._announce(plain(error)),
        )


def _count_word(count: int) -> str:
    words = ("Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten")
    return words[count] if count < len(words) else str(count)


__all__ = [
    "APP_NAME",
    "OTHER_COMMANDS",
    "TOOLS_ROWS",
    "ScribeDocumentsMixin",
    "palette_title",
    "scribe_wildcard",
]
