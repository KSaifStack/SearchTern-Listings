"""Multi-file atomic replacement for the published surface.

The feed, its manifest, the README pages, country pages and the legacy count
artifacts describe one generation. Writing them one at a time means a crash, a
disk-full error or a validation failure can leave a reader with half of a new
generation and half of an old one. `write_pairs` stages every file first, moves
the old files aside, then replaces all of them; any failure restores the old
files and removes the new ones.

`os.replace` is the only commit point, so a caller can test each stage by making
that function fail once.
"""
import contextlib
import os
import tempfile

# One replace attempt per file. Exposed so tests can describe a stage by name.
STAGE_STAGE = "stage"
STAGE_BACKUP = "backup"
STAGE_REPLACE = "replace"


def write_pairs(pairs):
    """Write ``[(path, text)]`` as one transaction.

    All temp files are staged and fsynced before any live file is touched. The
    old files are moved to ``.bak.<pid>`` names, then every temp file replaces
    its target. A failure in any stage removes newly written files and restores
    every backup, so the caller keeps the previous generation byte-identical.
    """
    staged = []
    backups = {}
    replaced = []
    try:
        for path, text in pairs:
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
            handle, temp = tempfile.mkstemp(
                dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp"
            )
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            staged.append((path, temp))

        for path, _temp in staged:
            if os.path.exists(path):
                backup = f"{path}.bak.{os.getpid()}"
                os.replace(path, backup)
                backups[path] = backup

        for path, temp in staged:
            os.replace(temp, path)
            replaced.append(path)

        for backup in backups.values():
            with contextlib.suppress(OSError):
                os.unlink(backup)
    except Exception:
        for path in replaced:
            with contextlib.suppress(OSError):
                os.unlink(path)
        for path, backup in backups.items():
            if os.path.exists(backup):
                os.replace(backup, path)
        raise
    finally:
        for _path, temp in staged:
            if os.path.exists(temp):
                os.unlink(temp)


def write_text(path, text):
    """Single-file convenience wrapper with the same guarantee."""
    write_pairs([(path, text)])
