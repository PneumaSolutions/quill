"""Play Sample beside the voice in the Convert dialog (Scribe PRD 7.3, part 3; 12).

The sample is Scribe's own recording of the chosen voice. It is fetched once
through the Scribe HTTP door (GATE-9) on a worker thread, kept in a temporary
folder for the session, and played through the family's shared audio engine
(:func:`quill.ui.audio.audio_engine.create_engine`), so it uses the app's
audio output like every other sound QUILL plays.

While it plays the button reads Stop Sample, and Escape stops the sample
without closing the dialog; otherwise Escape cancels as usual.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

PLAY_LABEL = "Pla&y Sample"
STOP_LABEL = "St&op Sample"

#: short name -> downloaded sample, for the session.
_SAMPLES: dict[str, Path] = {}
_FOLDER: list[Path] = []


def sample_file(short_name: str, data: bytes) -> Path:
    """Keep a downloaded sample for the session; returns its file."""
    if not _FOLDER:
        _FOLDER.append(Path(tempfile.mkdtemp(prefix="quill-scribe-voices-")))
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in short_name) or "voice"
    path = _FOLDER[0] / f"{safe}.mp3"
    path.write_bytes(data)
    _SAMPLES[short_name] = path
    return path


class VoiceSample:
    """The Play Sample button and the one sample it plays."""

    def __init__(self, owner: Any, host: Any, voice_name: Any) -> None:
        import wx

        self.wx = wx
        self.host = host
        self._voice_name = voice_name  # () -> (short name, has a sample)
        self._engine: Any = None
        self._playing = False
        self._wanted = ""
        self.button = wx.Button(owner, label=PLAY_LABEL)
        self.button.SetName("Play Sample")
        self.button.SetHelpText(
            "Hear the chosen voice read Scribe's sample. Press it again, or Escape, "
            "to stop. The sample comes from Scribe for Documents."
        )
        self.button.Bind(wx.EVT_BUTTON, lambda _e: self.toggle())

    @property
    def playing(self) -> bool:
        return self._playing or bool(self._wanted)

    def refresh(self) -> None:
        """Enable the button only for a voice that has a sample."""
        short_name, has_sample = self._voice_name()
        if self.playing and short_name != self._wanted:
            self.stop()
        self.button.Enable(bool(short_name) and has_sample or self.playing)

    def toggle(self) -> None:
        if self.playing:
            self.stop()
            return
        short_name, has_sample = self._voice_name()
        if not short_name or not has_sample:
            self.host._announce("This voice has no sample.")
            return
        self._wanted = short_name
        self._set_label(STOP_LABEL)
        cached = _SAMPLES.get(short_name)
        if cached is not None and cached.is_file():
            self._play(cached)
            return

        def _run(progress_callback: Any, cancellation_token: Any, **_kw: Any) -> Path:
            from quill.core.scribe.client import ScribeClient

            return sample_file(short_name, ScribeClient().voice_sample(short_name))

        def _done(_op: Any, path: Path) -> None:
            if self._wanted == short_name:
                self._play(path)

        def _failed(_op: Any, error: BaseException) -> None:
            from quill.core.scribe.model import plain

            if self._wanted == short_name:
                self.stop()
                self.host._announce(f"The voice sample could not be played: {plain(error)}")

        self.host._task_manager.submit(
            name="scribe-voice-sample", func=_run, on_success=_done, on_failure=_failed
        )

    def _play(self, path: Path) -> None:
        from quill.ui.audio.audio_engine import create_engine

        try:
            if self._engine is None:
                self._engine = create_engine(
                    self.button.GetParent(),
                    on_loaded=lambda _ms: self._start(),
                    on_finished=self.stop,
                    on_error=self._error,
                )
            if self._engine is None or not self._engine.load(str(path)):
                self._error("The voice sample could not be played.")
        except Exception:  # noqa: BLE001 - a sample must never break the dialog
            self._error("The voice sample could not be played.")

    def _start(self) -> None:
        if self._engine is not None and self._wanted:
            self._playing = True
            self._engine.play()

    def _error(self, message: str) -> None:
        self.stop()
        self.host._announce(message)

    def stop(self) -> None:
        self._wanted = ""
        self._playing = False
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:  # noqa: BLE001 - stopping a dead engine is not an error
                pass
        self._set_label(PLAY_LABEL)

    def release(self) -> None:
        """When the dialog closes: stop and let the engine go."""
        self.stop()
        engine, self._engine = self._engine, None
        for name in ("terminate", "close"):
            method = getattr(engine, name, None)
            if callable(method):
                try:
                    method()
                except Exception:  # noqa: BLE001
                    pass
                break

    def _set_label(self, label: str) -> None:
        try:
            if self.button.GetLabel() != label:
                self.button.SetLabel(label)
        except RuntimeError:
            pass  # the dialog closed meanwhile


__all__ = ["PLAY_LABEL", "STOP_LABEL", "VoiceSample", "sample_file"]
