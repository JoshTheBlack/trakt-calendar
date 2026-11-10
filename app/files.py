"""Writing a file another thread may be reading.

Every file the app keeps on disk — an account's pictures and their names, a
poster tile, a rendered share card or export — is written on a worker thread and
read by requests on others, so a write and a read of one file can overlap.
`Path.write_bytes` truncates before it writes: a reader landing in between reads
an empty or half-written file, and a share card served that way is a broken
picture an unfurler keeps. `write_whole` is the one way such a file is written.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

# How long a replace waits for a reader on Windows (below), all tries together.
REPLACE_TRIES = 20
REPLACE_PAUSE_SECONDS = 0.01


def write_whole(path: Path, data: bytes) -> None:
    """Write `data` as `path` whole: to a file of its own beside it, then moved
    over it in one step, so a reader opening `path` meanwhile reads all of the
    old file or all of the new, and a write that fails leaves the old one as it
    was. The spare's name starts with a dot and ends `.part`, so nothing that
    lists a directory by extension counts it.

    ON WINDOWS a file another thread has open cannot be replaced (Python opens
    files without FILE_SHARE_DELETE), and a reader holds one for the moment a
    picture takes to read, so the move is tried again for a moment before it
    gives up. On Linux, where the app is deployed, the first try always lands."""
    path.parent.mkdir(parents=True, exist_ok=True)
    spare = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        spare.write_bytes(data)
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(spare, path)
                return
            except PermissionError:
                if sys.platform != "win32" or attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(REPLACE_PAUSE_SECONDS)
    finally:
        spare.unlink(missing_ok=True)
