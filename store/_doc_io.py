"""Shared JSON-document reader for the app-global store modules.

`store.prompt_library`, `store.app_settings` and `store.schema_cache` are triplicated
single-file JSON stores with the same three-way persistence question: is the file
**absent** (fresh install -> seeds/defaults are correct), **readable** (parse it), or
**present but unreadable/corrupt** (must NOT be treated as empty)?

The last case is the dangerous one. On Windows a transient AV/indexer lock surfaces as a
`PermissionError` (an `OSError`) - the exact failure mode `store.project._atomic_write_json`
retries around. If a loader swallows that into "return the seed set", a subsequent mutating
op (save/delete/set) reads the degraded seed set, mutates one entry, and writes it back -
silently discarding every user entry the locked file actually held (M11).

`read_doc` makes the distinction explicit:
  * **absent** -> returns `None` (the caller uses its seeds/defaults; safe).
  * **readable** -> returns the parsed top-level object (a `dict`, else `{}`).
  * **present but unreadable or corrupt** -> raises `UnreadableStoreError`.

Read-only accessors catch `UnreadableStoreError` and fall back to seeds/defaults (a
degraded read is fine - it touches no disk). Mutating ops let it propagate, refusing to
persist a set derived from a file they couldn't actually read.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


class UnreadableStoreError(Exception):
    """A store file exists but could not be read (locked/permission-denied) or parsed.

    Distinct from absence: the file is there and may hold user data, so a mutating op must
    refuse to overwrite it rather than clobber that data with a seed/default set.
    """


def read_doc(path: str | Path) -> Optional[dict]:
    """Read a JSON store file, distinguishing absent from present-but-unreadable.

    Returns `None` when the file does not exist (caller should use seeds/defaults). Returns
    the parsed top-level object as a `dict` (or `{}` if the JSON root isn't an object) when
    it reads cleanly. Raises `UnreadableStoreError` when the file is present but can't be
    opened/read (e.g. a transient Windows AV/indexer `PermissionError`) or holds invalid
    JSON - so callers can tell "nothing here yet" apart from "don't clobber this". An
    EMPTY (0-byte) file counts as unreadable, not absent: it's most plausibly an
    interrupted/truncated write, exactly the state a mutating op must not overwrite blind.
    """
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise UnreadableStoreError(f"cannot read store file {Path(path)!s}: {e}") from e
    return doc if isinstance(doc, dict) else {}
