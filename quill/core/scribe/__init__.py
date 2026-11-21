"""Scribe for Documents in the QUILL family: the shared foundation (wx-free).

Built once for every app (Scribe PRD section 7):

* :mod:`.model` -- formats, stages, settings and the coded errors (section 11).
* :mod:`.http` -- the one HTTP door (GATE-9 sees every call).
* :mod:`.auth` -- sign-in with PKCE through the browser, and the tokens.
* :mod:`.client` -- the REST API: account, documents, settings, downloads.
* :mod:`.channel` -- a document's live Phoenix channel.
* :mod:`.queue` -- the family's one queue of jobs, shared through a file.
* :mod:`.runner` -- runs that queue, one document at a time.
* :mod:`.library` -- which document made which local file; where downloads go.

Powered by Scribe for Documents (Pneuma Solutions, scribeit.io).
"""

from __future__ import annotations
