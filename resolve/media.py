"""Bring media a Resolve project names into Splice when the file is on this computer."""
import atexit
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import media_links
from media_proxy import SCAN_LIMIT, _text_art
import raw_decode

PROBES = max(1, min(8, os.cpu_count() or 1))
# Linking is bounded as a whole, not per file. 45 s leaves room for FFmpeg's own 25 s probe timeout
# on one slow file while keeping an open from waiting minutes on a dead drive; 2,000 distinct files
# probed is several times the media a feature's timelines use, and probes at about 20 files a second
# on a local disk (8 at a time), so it fits in the time limit. Looking a path up is cheap and only
# bounded by time and MAX_LOOKUPS: a project naming thousands of missing files still has its real
# ones checked.
LINK_SECONDS = 45.0
MAX_LINK_FILES = 2_000
MAX_LOOKUPS = 50_000
PROBE_SECONDS = 25.0
MAX_PATH = 4096
# Paths on one volume (the first two parts of a path) take at most this share of the workers at a
# time, so a hung network mount cannot hold every worker while files elsewhere wait.
PER_VOLUME = max(1, PROBES // 2)
# Looking paths up may take at most this share of the time, so files found are probed even while
# lookups on a hung volume never return.
LOOKUP_SHARE = 1 / 3
_children = set()
_children_lock = threading.Lock()
# Workers of every open share one cap: a lookup stuck in a filesystem call that never returns (a
# hung network mount) keeps its thread, so at most MAX_THREADS lookup threads live at once, and a
# volume with a lookup still unanswered from an earlier open gets no new one until it answers:
# each hung volume holds at most PER_VOLUME threads however often a project is opened.
MAX_THREADS = 4 * PROBES
_flight = threading.Lock()
_alive = 0          # lookup threads alive (all opens)
_unanswered = {}    # volume -> work items still running after their open's deadline
HUNG_MESSAGE = ('its drive did not answer when Splice last looked; Splice looks again once that drive answers '
                '(reconnect it, then open the project again)')


def hung(volume_key):
    """Whether a lookup on this volume from an earlier open has not returned yet."""
    with _flight:
        return _unanswered.get(volume_key, 0) > 0


def threads_alive():
    with _flight:
        return _alive


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


def _reap(processes):
    for process in processes:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


def stop_probes():
    """Kill and reap every probe still running (a deadline passed, or the server is exiting)."""
    with _children_lock:
        running = list(_children)
        _children.clear()
    _reap(running)


atexit.register(stop_probes)


def _probe(path, deadline):
    """None when Splice can edit the file, else its offline result. The FFprobe child is ours:
    killed and reaped when the open's deadline passes (or the server exits), never orphaned. The
    same check as media_proxy.decodable: text drawn as a picture is refused, Camera RAW files are
    probed via raw_decode, and regular files need a picture or sound stream FFmpeg decodes."""
    name = Path(path).name
    if raw_decode.is_raw_candidate(path, name):
        try:
            raw_decode.probe_raw(path)
            return None
        except (OSError, ValueError) as error:
            return _offline('not-media', str(error))
    left = min(PROBE_SECONDS, deadline - time.monotonic())
    if left <= 0:
        return _offline('unchecked', 'time ran out before it was looked at')
    try:
        process = subprocess.Popen(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-show_entries',
                                    'stream=codec_type,codec_name:format=format_name', '-of', 'json', 'file:' + str(path)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as error:
        return _offline('not-media', f'FFprobe could not run ({error.strerror or error})')
    with _children_lock:
        _children.add(process)
    try:
        output, _ = process.communicate(timeout=left)
    except subprocess.TimeoutExpired:
        _reap([process])
        return _offline('unchecked', f'reading {name} timed out; its drive may be slow or disconnected')
    finally:
        with _children_lock:
            _children.discard(process)
    try:
        data = json.loads(output[:SCAN_LIMIT]) if process.returncode == 0 else {}
    except ValueError:
        data = {}
    streams = [s for s in data.get('streams', []) if isinstance(s, dict)] if isinstance(data, dict) else []
    fmt = data.get('format') if isinstance(data, dict) and isinstance(data.get('format'), dict) else {}
    if _text_art(fmt, streams) or not any(s.get('codec_type') in ('video', 'audio') for s in streams):
        return _offline('not-media', f'The installed FFmpeg cannot decode {name}')
    return None


def _volume(item):
    text = str(item[0] if isinstance(item, tuple) else item)
    parts = [p for p in text.replace('\\', '/').split('/') if p]
    return '/'.join(parts[:2])


def _bounded(work, items, deadline, volume=None, on_hung=None):
    """``work(item)`` for each item, ``PROBES`` at a time, until ``deadline`` (time.monotonic()).

    Returns {index: result} for the items finished in time. The workers are daemon threads, so one
    stuck in a filesystem call that never returns (a hung network mount) neither delays this past
    the deadline nor keeps the server from exiting. With ``volume(item)``, at most
    :data:`PER_VOLUME` items of one volume run at a time, so files on other volumes are still
    checked while one volume hangs; an item on a volume an earlier call left unanswered is not
    worked at all (its result is ``on_hung``), and no more than :data:`MAX_THREADS` workers live
    across all calls, so repeated opens of a project on a hung drive do not pile up threads."""
    global _alive
    results = {}
    if not items:
        return results
    lock = threading.Condition()
    queue = list(enumerate(items))
    busy = {}
    running = {}  # index -> volume key of the items being worked
    late = set()  # indexes still running when the deadline passed (their volume is now unanswered)

    def take():
        for position, (index, item) in enumerate(queue):
            key = volume(item) if volume else None
            if key is not None and hung(key):
                del queue[position]
                results[index] = on_hung
                return take()
            if key is None or busy.get(key, 0) < PER_VOLUME:
                del queue[position]
                if key is not None:
                    busy[key] = busy.get(key, 0) + 1
                running[index] = key
                return index, item, key
        return None

    def worker():
        global _alive
        try:
            while True:
                with lock:
                    while True:
                        if not queue or time.monotonic() >= deadline:
                            lock.notify_all()
                            return
                        taken = take()
                        if taken is not None:
                            break
                        if not queue:
                            lock.notify_all()
                            return
                        lock.wait(min(0.2, max(0.0, deadline - time.monotonic())))
                    index, item, key = taken
                value = work(item)
                with lock:
                    results[index] = value
                    running.pop(index, None)
                    if key is not None:
                        busy[key] -= 1
                    if index in late and key is not None:
                        with _flight:
                            _unanswered[key] -= 1
                            if _unanswered[key] <= 0:
                                del _unanswered[key]
                    lock.notify_all()
        finally:
            with _flight:
                _alive -= 1
    with _flight:
        count = max(0, min(PROBES, len(items), MAX_THREADS - _alive))
        _alive += count
    threads = [threading.Thread(target=worker, name='splice-resolve-link', daemon=True) for _ in range(count)]
    for thread in threads:
        thread.start()
    with lock:
        while len(results) < len(items):
            left = deadline - time.monotonic()
            if left <= 0 or not any(thread.is_alive() for thread in threads):
                break
            lock.wait(min(left, 0.5))
        for index, key in running.items():
            late.add(index)
            if key is not None:
                with _flight:
                    _unanswered[key] = _unanswered.get(key, 0) + 1
        lock.notify_all()  # idle workers see the deadline and end now
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
    limit = f'Splice checks at most {max_files:,} files for {seconds:g} s when it opens a project'
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
    for text in texts[MAX_LOOKUPS:]:
        for index in wanted[text]:
            results[index] = _offline('unchecked', limit)
    texts = texts[:MAX_LOOKUPS]
    looked = _bounded(lambda text: _look(Path(text)), texts, time.monotonic() + seconds * LOOKUP_SHARE, _volume,
                      _offline('unchecked', HUNG_MESSAGE))
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
    for identity in identities[max_files:]:
        for index in named[identity]:
            results[index] = _offline('unchecked', limit)
    identities = identities[:max_files]
    try:
        probed = _bounded(lambda identity: _probe(files[identity], deadline), identities, deadline,
                          lambda identity: _volume(files[identity]), _offline('unchecked', HUNG_MESSAGE))
    finally:
        stop_probes()
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
            if time.monotonic() >= deadline:
                raise TimeoutError(limit)
        linked = media_links.link_many([files[identity] for identity in playable], validate=validate)
        for identity, result in zip(playable, linked):
            if 'src' in result:
                outcome = result
            elif result.get('error') == limit:
                outcome = _offline('unchecked', limit)
            else:
                outcome = _offline('refused', result.get('error'))
            for index in named[identity]:
                results[index] = outcome
    return results
