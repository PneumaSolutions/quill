"""Downloading a finished Scribe output: resumable, verified, never spliced (wx-free).

The second half of :meth:`quill.core.scribe.client.ScribeClient.download`: the
API answers with a 302 to a presigned storage URL, fetched here -- through the
one HTTP door, :func:`quill.core.scribe.http.request`, without the bearer
token -- into a partial file that is moved into place only when it is whole
(PRD 7.6; independent review Q5, 2026-11-21).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from http.client import HTTPException
from pathlib import Path
from typing import Any

from quill.core.scribe import http
from quill.core.scribe.model import ScribeCancelled, ScribeOffline, ScribeSaveFailed

logger = logging.getLogger(__name__)

ProgressFn = Callable[[int, int], None]
CancelFn = Callable[[], bool]

_CHUNK = 256 * 1024
#: A download tries this many requests in all, and gives up after this many in
#: a row that brought no new bytes; the partial file stays for Retry.
_MAX_REQUESTS = 12
_MAX_STALLS = 4
_CONTENT_RANGE = re.compile(r"bytes\s+(?:(\d+)-\d+|\*)/(\d+|\*)", re.I)


def fetch_resumable(
    url: str,
    folder: Path,
    final_name: str,
    *,
    part_key: str = "",
    on_progress: ProgressFn | None = None,
    cancel_requested: CancelFn | None = None,
) -> Path:
    """Fetch ``url`` into ``folder`` through a partial file, resuming after drops.

    Nothing is renamed into place until every byte the server announced is
    on disk (independent review Q5): a stream that ends early is resumed
    with ``Range``, and after too many tries the job fails with the partial
    kept for Retry. A partial is resumed only when its sidecar says which
    download it belongs to (the server's ``ETag`` and total); the server's
    ``Content-Range`` must start where the partial ends, and ``If-Range``
    makes a changed file come back whole. A 416 on a partial that is
    already complete finishes it; on anything else the partial starts over.
    The finished file is moved into place in one ``os.replace``.
    """
    from quill.core.scribe.client import safe_filename, unique_target

    key = safe_filename(part_key) if part_key else ""
    partial = folder / (f"{final_name}.{key}.part" if key else f"{final_name}.part")
    meta_path = partial.with_name(partial.name + ".json")
    meta = _read_meta(meta_path) if partial.exists() else {}
    if partial.exists() and not meta:
        _discard(partial, meta_path)  # no record of whose it is: never splice it
    stalls = 0
    complete = False
    for attempt in range(_MAX_REQUESTS):
        done = partial.stat().st_size if partial.exists() else 0
        headers: dict[str, str] = {}
        if done:
            headers["Range"] = f"bytes={done}-"
            if meta.get("etag"):
                headers["If-Range"] = str(meta["etag"])
        try:
            response = http.request("GET", url, headers=headers, timeout=300.0, stream=True)
        except ScribeOffline:
            stalls += 1
            if stalls >= _MAX_STALLS or attempt == _MAX_REQUESTS - 1:
                raise
            continue
        if response.status == 416:
            _close(response)
            total = meta.get("total") or _range_total(response.header("content-range"))
            if done and total and done == total:
                complete = True  # finished before the app stopped; only the rename was left
                break
            _discard(partial, meta_path)
            meta = {}
            continue
        if response.status not in (200, 206) or response.stream is None:
            http.raise_for(response, what="downloading")
            raise ScribeSaveFailed("The download did not start.", folder=str(folder))
        if response.status == 206:
            start, total = _content_range(response.header("content-range"))
            known = meta.get("total")
            if (start is not None and start != done) or (total and known and total != known):
                _close(response)
                _discard(partial, meta_path)  # not the bytes this partial needs
                meta = {}
                continue
            if total is None:
                length = response.header("content-length")
                total = done + int(length) if length.isdigit() else None
        else:
            done = 0  # the whole file, from the first byte
            length = response.header("content-length")
            total = int(length) if length.isdigit() else None
        meta = {"etag": response.header("etag"), "total": total}
        _write_meta(meta_path, meta)
        before = done
        ended, done = _copy_stream(
            response,
            partial,
            done,
            total,
            target_name=final_name,
            folder=folder,
            on_progress=on_progress,
            cancel_requested=cancel_requested,
        )
        if total is None:
            if ended:
                complete = True  # no length announced: a clean end is all there is
                break
        elif done == total:
            complete = True
            break
        elif done > total:
            _discard(partial, meta_path)  # more than announced: not trustworthy
            meta = {}
            continue
        stalls = 0 if done > before else stalls + 1
        if stalls >= _MAX_STALLS:
            break
        logger.info("Scribe download dropped; resuming attempt=%s", attempt + 1)
    if not complete:
        raise ScribeSaveFailed(
            f"The download of {final_name} stopped before the end. Retry carries on "
            "from where it stopped.",
            folder=str(folder),
        )
    target = unique_target(folder, final_name)
    try:
        os.replace(partial, target)
    except OSError as exc:
        raise ScribeSaveFailed(
            f"QUILL could not save {target.name} in {folder}: {exc.strerror or exc}.",
            folder=str(folder),
        ) from None
    meta_path.unlink(missing_ok=True)
    return target


def _copy_stream(
    response: http.Response,
    partial: Path,
    done: int,
    total: int | None,
    *,
    target_name: str,
    folder: Path,
    on_progress: ProgressFn | None,
    cancel_requested: CancelFn | None,
) -> tuple[bool, int]:
    """Append the body to ``partial``. (ended cleanly, bytes now on disk).

    A drop while reading is not an error here (the caller resumes); a
    failure to write is, at once: a full disk does not get better.
    """
    assert response.stream is not None
    try:
        handle = open(partial, "ab" if done else "wb")  # noqa: SIM115 - closed below
    except OSError as exc:
        _close(response)
        raise ScribeSaveFailed(
            f"QUILL could not save {target_name} in {folder}: {exc.strerror or exc}.",
            folder=str(folder),
        ) from None
    ended = False
    try:
        with handle, response.stream as stream:
            while True:
                if cancel_requested is not None and cancel_requested():
                    raise ScribeCancelled("The download was stopped.")
                try:
                    chunk = stream.read(_CHUNK)
                except (OSError, HTTPException, ValueError):
                    break  # dropped mid-body; resumed by the caller
                if not chunk:
                    ended = True
                    break
                try:
                    handle.write(chunk)
                except OSError as exc:
                    raise ScribeSaveFailed(
                        f"QUILL could not save {target_name} in {folder}: {exc.strerror or exc}.",
                        folder=str(folder),
                    ) from None
                done += len(chunk)
                if on_progress is not None:
                    on_progress(done, total or 0)
    except (OSError, HTTPException):
        pass  # closing a dropped stream
    return ended, done


def _content_range(value: str) -> tuple[int | None, int | None]:
    """``bytes 100-999/1000`` -> (100, 1000); unknown parts are None."""
    match = _CONTENT_RANGE.search(value or "")
    if not match:
        return None, None
    start = int(match.group(1)) if match.group(1) else None
    total = int(match.group(2)) if match.group(2).isdigit() else None
    return start, total


def _range_total(value: str) -> int | None:
    return _content_range(value)[1]


def _read_meta(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(path: Path, meta: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(meta), encoding="utf-8")
    except OSError:
        pass  # without it a later resume simply starts over


def _discard(partial: Path, meta_path: Path) -> None:
    for path in (partial, meta_path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _close(response: http.Response) -> None:
    if response.stream is not None:
        try:
            response.stream.close()
        except (OSError, HTTPException):
            pass


__all__ = ["fetch_resumable"]
