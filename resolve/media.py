"""Bring media a Resolve project names into Splice when the file is on this computer."""
import os
import threading
import time
from pathlib import Path

import media_links
from media_proxy import ProbeTimeout, decodable

PROBES = max(1, min(8, os.cpu_count() or 1))
# Linking is bounded as a whole, not per file. 45 s leaves room for FFmpeg's own 25 s probe timeout
# on one slow file while keeping an open from waiting minutes on a dead drive; 2,000 distinct files
# is several times the media a feature's timelines use, and probes at about 20 files a second on a
# local disk (8 at a time), so it fits in the time limit.
LINK_SECONDS = 45.0
MAX_LINK_FILES = 2_000
MAX_PATH = 4096


def _offline(reason, message=None):
    return {'offline': reason, **({'message': message} if message else {})}


def _usable_text(text):
    """Whether ``text`` may be looked up at all: an absolute path of sane length with a media
    extension. Nothing here touches the filesystem."""
    if not isinstance(text, str) or not text or len(text) > MAX_PATH or '\x00' in text:
        return None
    return Path(text) if os.path.isabs(text) else None


def _look(path):
    """The real file behind ``path``: (resolved path, identity) or an offline result."""
    try:
        # strict: every folder on the way must exist, as when the file is opened (a lexical
        # ".." past a missing folder must not reach a file beside it).
        resolved = path.resolve(strict=True)
        if resolved.suffix.lower() not in media_links.MEDIA_EXTENSIONS:
            return _offline('not-media')
        # A regular file only: FFmpeg would wait forever on a FIFO or a device.
        if not resolved.is_file():
            return _offline('missing')
        info = resolved.stat()
    except (OSError, RuntimeError, ValueError):
        return _offline('missing')
    return str(resolved), (info.st_dev, info.st_ino)


def _probe(path):
    """None when FFmpeg can edit the file, else its offline result."""
    try:
        decodable(Path(path), Path(path).name)
    except ProbeTimeout as error:
        return _offline('unchecked', str(error))
    except (ValueError, OSError) as error:
        return _offline('not-media', str(error))
    return None


def _bounded(work, items, deadline):
    """``work(item)`` for each item, ``PROBES`` at a time, until ``deadline`` (time.monotonic()).

    Returns {index: result} for the items finished in time. The workers are daemon threads, so one
    stuck in a filesystem call that never returns (a hung network mount) neither delays this past
    the deadline nor keeps the server from exiting."""
    results = {}
    if not items:
        return results
    lock = threading.Condition()
    queue = list(enumerate(items))
    queue.reverse()

    def worker():
        while True:
            with lock:
                if not queue or time.monotonic() >= deadline:
                    lock.notify_all()
                    return
                index, item = queue.pop()
            value = work(item)
            with lock:
                results[index] = value
                lock.notify_all()
    threads = [threading.Thread(target=worker, name='splice-resolve-link', daemon=True)
               for _ in range(min(PROBES, len(items)))]
    for thread in threads:
        thread.start()
    with lock:
        while len(results) < len(items):
            left = deadline - time.monotonic()
            if left <= 0 or not any(thread.is_alive() for thread in threads):
                break
            lock.wait(min(left, 0.5))
        return dict(results)


def link_local(paths, *, seconds=LINK_SECONDS, max_files=MAX_LINK_FILES):
    """Reference in place the files in ``paths`` that are media on this computer.

    Returns one result per path: ``media_links``' source description (``src``, ``sourcePath``,
    ``fingerprint``, ...) for a file that was linked, else ``{'offline': reason}`` with reason
    ``missing`` (nothing at that path here, or not a regular file), ``not-media`` (no media
    extension, or FFmpeg cannot decode it), ``unchecked`` (past the time or file limit) or
    ``refused`` (the link registry would not take it), and sometimes a ``message``.

    Nothing is copied and nothing is written beside the original. A file is registered only when
    it is an absolute path to a regular file with a media extension and the registry's own FFmpeg
    probe (``decodable``) accepts it, so a project naming ``/etc/passwd`` or a key file registers
    nothing. Each distinct path is looked up once and each real file (however many paths or
    symlinks name it) probed once; the whole step stops after ``seconds`` or ``max_files``
    distinct paths, and the registry is written once for the project.
    """
    deadline = time.monotonic() + seconds
    results = [None] * len(paths)
    limit = f'Splice looks for at most {max_files:,} files for {seconds:g} s when it opens a project'
    wanted = {}  # path text -> indexes
    for index, text in enumerate(paths):
        path = _usable_text(text)
        if path is None:
            results[index] = _offline('missing')
        elif path.suffix.lower() not in media_links.MEDIA_EXTENSIONS:
            results[index] = _offline('not-media')
        else:
            wanted.setdefault(text, []).append(index)
    texts = list(wanted)
    for text in texts[max_files:]:
        for index in wanted[text]:
            results[index] = _offline('unchecked', limit)
    texts = texts[:max_files]
    looked = _bounded(lambda text: _look(Path(text)), texts, deadline)
    files = {}  # identity of a real file -> its resolved path
    named = {}  # identity -> the indexes of every path naming it
    for number, text in enumerate(texts):
        found = looked.get(number, _offline('unchecked', limit))
        if isinstance(found, dict):
            for index in wanted[text]:
                results[index] = found
            continue
        resolved, identity = found
        files.setdefault(identity, resolved)
        named.setdefault(identity, []).extend(wanted[text])
    identities = list(files)
    probed = _bounded(lambda identity: _probe(files[identity]), identities, deadline)
    playable = []
    for number, identity in enumerate(identities):
        if number in probed and probed[number] is None:
            playable.append(identity)
            continue
        outcome = probed.get(number) or _offline('unchecked', limit)
        for index in named[identity]:
            results[index] = outcome
    if playable:
        accepted = {files[identity] for identity in playable}

        def validate(path, name):
            if str(path) not in accepted:
                raise ValueError(f'{name} was not checked')
        linked = media_links.link_many([files[identity] for identity in playable], validate=validate)
        for identity, result in zip(playable, linked):
            outcome = result if 'src' in result else _offline('refused', result.get('error'))
            for index in named[identity]:
                results[index] = outcome
    return results
