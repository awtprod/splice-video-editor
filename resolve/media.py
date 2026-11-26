"""Bring media a Resolve project names into Splice when the file is on this computer."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import media_links
from media_proxy import decodable

PROBES = max(1, min(8, os.cpu_count() or 1))


def link_local(paths):
    """Reference in place the files in ``paths`` that are media on this computer.

    Returns one result per path: ``media_links``' source description (``src``,
    ``sourcePath``, ``fingerprint``, ...) for a file that was linked, else ``None``.
    Nothing is copied and nothing is written beside the original. A file is
    registered only when it is an absolute path to a regular file with a media
    extension and the registry's own FFmpeg probe (``decodable``) accepts it, so a
    project naming ``/etc/passwd`` or a key file registers nothing. A path from
    another machine (``E:\\Shoot\\A001.mov`` on Linux), a missing file or anything
    else stays offline and is relinked by the person editing. The registry is
    written once for the whole project.
    """
    candidates = []
    for index, text in enumerate(paths):
        try:
            if not isinstance(text, str) or not text or len(text) > 4096 or '\x00' in text:
                continue
            path = Path(text)
            if not os.path.isabs(text) or path.suffix.lower() not in media_links.MEDIA_EXTENSIONS:
                continue
            resolved = path.resolve(strict=True)
            if resolved.suffix.lower() not in media_links.MEDIA_EXTENSIONS or not resolved.is_file():
                continue
        except (OSError, ValueError, RuntimeError):
            continue
        candidates.append((index, str(resolved)))
    results = [None] * len(paths)
    if candidates:
        # The registry's FFmpeg check, run for the whole project a few probes at a time; link_many
        # then consults the outcome per file and writes the registry once.
        def probe(path):
            try:
                decodable(Path(path), Path(path).name)
            except Exception as error:  # noqa: BLE001 - reported per file by link_many
                return error
            return None
        with ThreadPoolExecutor(max_workers=min(PROBES, len(candidates))) as pool:
            outcome = dict(zip((path for _, path in candidates), pool.map(probe, (path for _, path in candidates))))

        def validate(path, name):
            error = outcome.get(str(path), ValueError(f'{name} was not checked'))
            if error is not None:
                raise error if isinstance(error, (ValueError, OSError)) else ValueError(str(error))
        linked = media_links.link_many([path for _, path in candidates], validate=validate)
        for (index, _), result in zip(candidates, linked):
            if 'src' in result:
                results[index] = result
    return results
