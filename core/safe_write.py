"""Writing the app's own files so that a full disk or a crash cannot cost the copy that is already there."""
from pathlib import Path

import plat_fs


def write_text(path, text: str) -> None:
    """Write `text` to `path` through a temporary file and a rename. A plain write_text empties the file first, so a
    disk that fills up midway leaves an empty or cut-off file, and the next start reads that as having nothing saved.
    Here the old file stays until the new one is complete. A failure is raised after the temporary file is removed."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        plat_fs.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
