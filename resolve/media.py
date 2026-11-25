"""Bring media a Resolve project names into Splice when the file is on this computer."""
import hashlib
import os
from pathlib import Path
from urllib.parse import quote

from premiere import MEDIA_ROOT

# The extensions Splice's media upload accepts.
MEDIA_SUFFIXES = frozenset({'.mp4', '.mov', '.mkv', '.webm', '.wav', '.mp3', '.m4a', '.jpg', '.jpeg', '.png',
                            '.aac', '.ogg', '.flac', '.avi', '.mxf', '.aiff', '.m4v', '.bmp'})


def link_local(path_text, media_root=None):
    """A playback URL for ``path_text`` if it is a media file on this computer, else ''.

    The file is hard-linked into Splice's media folder (no copy, nothing written beside the
    original). A path from another machine (``E:\\Shoot\\A001.mov`` on a Mac), a file on another
    volume than Splice's media folder, or anything that is not a regular media file stays offline
    and is relinked by the person editing.
    """
    root = Path(media_root) if media_root is not None else MEDIA_ROOT
    try:
        path = Path(path_text)
        if not path.is_absolute() or path.suffix.lower() not in MEDIA_SUFFIXES:
            return ''
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or resolved.suffix.lower() not in MEDIA_SUFFIXES:
            return ''
        info = resolved.stat()
        key = hashlib.sha256(f'{resolved}|{info.st_size}|{info.st_mtime_ns}'.encode('utf-8', 'surrogatepass'))
        directory = root / key.hexdigest()[:32]
        destination = directory / resolved.name
        if not destination.is_file():
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.link(resolved, destination)
            except FileExistsError:
                pass
            except OSError:
                try:
                    directory.rmdir()
                except OSError:
                    pass
                return ''
        return '/media/' + directory.name + '/' + quote(destination.name)
    except (OSError, ValueError, RuntimeError):
        return ''
