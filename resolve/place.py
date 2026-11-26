"""Put a new project into a Resolve disk library without ever touching an existing one.

Follows ai-agent-resolve ``src/aar/drp/libwrite/place.py`` at commit
baa82027bc9212d5b64572b70806f0d69f8812fa (same owner), whose placed projects Resolve listed and
opened:

* The folder must be a disk library: ``Resolve Projects/Users/<user>/Projects`` (refused
  otherwise). No folder below the library may be a symbolic link (or a Windows junction), and
  the resolved ``Projects`` folder must lie inside the resolved library.
* The name must be new: compared after Unicode normalization and case folding (Windows'
  comparison included), and one Windows and the share can hold (no ``\\ / : * ? " < > |``, no
  control character, no reserved device name, no trailing dot or space, no leading space or
  dot, at most 200 characters and 255 UTF-8 bytes).
* The project is built in ``Resolve Projects/.splice-staging/build-<random>/`` (the same file
  system as the projects), checked, given the modes and owner of the library's existing
  project folders, and moved into ``Projects/<name>`` with one rename that never replaces
  anything (Linux ``renameat2(RENAME_NOREPLACE)``, else a rename right after a fresh check,
  which cannot replace a project folder that holds a file). Two saves of names that fold alike
  are serialised by an exclusive lock file; the second fails cleanly.
* On every failure path the build folder and the lock are removed, so nothing is left in the
  library; what cannot be removed is named in the error. A save that is killed cannot clean up,
  so every save first removes what interrupted saves left (:func:`clean_stale`): only Splice's
  own ``build-*`` folders (holding Splice's marker file, or empty) and ``lock-*`` files
  (holding Splice's lock text, or empty), only once they are older than
  :data:`STALE_SECONDS` and no live save holds their lock (each save holds an ``flock`` on its
  lock file, which names its build folder), never following a link; a lock that is a link or
  cannot be opened is ignored. Where ``flock`` does not work (Windows, some network file
  systems) entries are removed by age only, after :data:`STALE_SECONDS_UNLOCKABLE`. The
  staging folder itself goes when it is empty. The server does the same at start for the libraries it saved into.
"""
import errno
import os
import re
import secrets
import shutil
import stat
import sys
import time
import unicodedata
from pathlib import Path

from .source import ResolveError

STAGING = '.splice-staging'
#: Staging entries left by an interrupted save are removed once they are this old and no live
#: save holds their lock (where the file system has no working ``flock``, only after a day).
STALE_SECONDS = 30
STALE_SECONDS_UNLOCKABLE = 24 * 3600
_BUILD = re.compile(r'build-[0-9a-f]{16}\Z')
_LOCK = re.compile(r'lock-[0-9a-f]{32}\Z')
#: The marker file every build folder holds from right after it is made (an empty build folder,
#: from a save killed before it wrote the marker, holds nothing and is removed too). A folder
#: with Splice's naming but without the marker is never removed.
MARKER = '.splice-build'
_MARKER_TEXT = b'Splice save in progress\n'
#: What a lock file holds: this prefix and the name of its build folder.
_LOCK_TEXT = 'splice-save '
MAX_NAME = 200
_FORBIDDEN = set('\\/:*?"<>|')
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {f'{p}{n}' for p in ('COM', 'LPT') for n in
                                                                   list('123456789') + ['¹', '²', '³']}
_UNAVAILABLE = frozenset({errno.ENOSYS, errno.EINVAL, errno.EPERM, errno.EOPNOTSUPP, getattr(errno, 'ENOTSUP', 95)})
_HIDDEN = {0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180E, 0x3164, 0xFEFF, 0xFFA0}


class PlaceError(ResolveError):
    """The library cannot take the project; nothing was placed."""


def fold_name(name):
    """The form two folder names share when a share may treat them as one (it may fold more
    names together than a given share does, never fewer)."""
    folded = unicodedata.normalize('NFD', name).casefold().upper().casefold()
    return unicodedata.normalize('NFC', unicodedata.normalize('NFD', folded))


def check_name(name):
    """``name`` if a new project folder may be named so; PlaceError saying why otherwise."""
    if not isinstance(name, str) or not name:
        raise PlaceError('Enter a name for the new project.')
    if len(name) > MAX_NAME or len(name.encode('utf-8', 'surrogatepass')) > 255 \
            or len(name.encode('utf-16-le', 'surrogatepass')) // 2 > 255:
        raise PlaceError(f'The project name is longer than {MAX_NAME} characters.')
    if unicodedata.normalize('NFC', name) != name:
        raise PlaceError('The project name is not in composed Unicode form (NFC); type it again.')
    for char in name:
        code = ord(char)
        if code < 32 or code == 127 or 0x80 <= code < 0xA0 or 0xD800 <= code <= 0xDFFF:
            raise PlaceError('The project name holds a control character.')
        if char in _FORBIDDEN:
            raise PlaceError('A project name cannot hold \\ / : * ? " < > | (Windows and Resolve refuse them).')
        if code in _HIDDEN or unicodedata.category(char) in ('Cf', 'Zl', 'Zp') or \
                (unicodedata.category(char) == 'Zs' and char != ' '):
            raise PlaceError(f'The project name holds a character a reader cannot see (U+{code:04X}).')
    if name[0] in ' .' or name[-1] in ' .':
        raise PlaceError('A project name cannot start with a space or a dot, or end with a space or a dot.')
    if name.split('.')[0].strip().upper() in _RESERVED:
        raise PlaceError(f'“{name}” is a name Windows reserves for a device.')
    if name.casefold() == 'project.db':
        raise PlaceError('Choose another project name.')
    return name


def _no_link(path, what):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise PlaceError(f'This folder is not a Resolve disk library: {what} is missing.') from None
    except OSError as error:
        raise PlaceError(f'The library cannot be read ({what}: {error.strerror or error}).') from None
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise PlaceError(f'{what} is a link; Splice does not follow links out of a library.')
    if not stat.S_ISDIR(info.st_mode):
        raise PlaceError(f'This folder is not a Resolve disk library: {what} is not a folder.')
    return info


_SQLITE = b'SQLite format 3\x00'
#: Project folders looked at for a Resolve ``Project.db`` when a user folder has no ``User.db``.
EVIDENCE_SCAN = 64


def _resolve_database(path, table):
    """Whether ``path`` is a regular file (not a link) holding an SQLite database with ``table``:
    read without changing it (read-only, immutable)."""
    import sqlite3
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 512:
            return False
        with open(path, 'rb') as handle:
            if handle.read(16) != _SQLITE:
                return False
        conn = sqlite3.connect(f'file:{Path(path).as_posix()}?mode=ro&immutable=1', uri=True)
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                            (table,)).fetchone() is None:
                return False
            # A project database Resolve made holds its project's row.
            return table != 'SM_Project' or conn.execute('SELECT 1 FROM "SM_Project" LIMIT 1').fetchone() is not None
        finally:
            conn.close()
    except (OSError, ValueError, Exception):  # sqlite3.Error and anything a crafted file raises
        return False


def made_by_resolve(user_folder, projects):
    """Evidence that Resolve made this user's folder of a disk library: the ``User.db`` Resolve
    keeps beside ``Projects`` (an SQLite database with its ``SM_User`` table), or at least one
    project folder holding a ``Project.db`` with Resolve's ``SM_Project`` table."""
    if _resolve_database(os.path.join(user_folder, 'User.db'), 'SM_User'):
        return True
    try:
        entries = sorted(os.scandir(projects), key=lambda e: e.name)[:EVIDENCE_SCAN]
    except OSError:
        return False
    for entry in entries:
        if not entry.name.startswith('.') and entry.is_dir(follow_symlinks=False) and \
                _resolve_database(os.path.join(entry.path, 'Project.db'), 'SM_Project'):
            return True
    return False


def library_users(library):
    """``(library, Resolve Projects folder, {user: Projects folder})`` of a disk library."""
    library = Path(library).expanduser()
    if not library.is_dir():
        raise PlaceError('Choose the library folder (the folder that holds “Resolve Projects”).')
    root = library / 'Resolve Projects'
    _no_link(root, '“Resolve Projects”')
    _no_link(root / 'Users', '“Resolve Projects/Users”')
    users, unproven = {}, []
    try:
        entries = sorted(os.scandir(root / 'Users'), key=lambda e: e.name)
    except OSError as error:
        raise PlaceError(f'The library cannot be read ({error.strerror or error}).') from None
    for entry in entries[:200]:
        if entry.name.startswith('.') or not entry.is_dir(follow_symlinks=False):
            continue
        projects = Path(entry.path) / 'Projects'
        try:
            _no_link(projects, f'the Projects folder of user “{entry.name}”')
        except PlaceError:
            continue
        if not made_by_resolve(entry.path, projects):
            unproven.append(entry.name)
            continue
        users[entry.name] = projects
    if not users:
        if unproven:
            raise PlaceError('This folder has the layout of a Resolve disk library, but nothing in it shows that '
                             'Resolve made it (no user database, no Resolve project). Create the library in Resolve '
                             'first (Project Manager → Project Libraries → Add Project Library), then save into it.')
        raise PlaceError('This folder is not a Resolve disk library: no user in it has a Projects folder.')
    real_library = library.resolve()
    for user, projects in users.items():
        if real_library not in projects.resolve().parents:
            raise PlaceError(f'The Projects folder of user “{user}” lies outside the library.')
    return library, root, users


def project_db_inside(path, projects):
    """``path`` resolved, when it names ``<project folder>/Project.db`` directly inside the
    ``Projects`` folder ``projects`` (no link anywhere below it, nothing outside it); None
    otherwise. Callers answer every None the same way, so the answer tells nothing about files
    outside the library."""
    try:
        path = Path(str(path))
        if not path.is_absolute() or path.name != 'Project.db' or '..' in path.parts:
            return None
        base = Path(projects).resolve(strict=True)
        for part in (path.parent, path):
            info = os.lstat(part)
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                return None
        real = path.resolve(strict=True)
        if real.parent.parent != base or not stat.S_ISREG(os.lstat(real).st_mode):
            return None
        return real
    except (OSError, ValueError, RuntimeError):
        return None


def pick_user(users, user=None):
    if user is not None:
        if user not in users:
            raise PlaceError(f'The library has no user “{user}” with a Projects folder.')
        return user
    if len(users) != 1:
        raise PlaceError('The library has several users: choose which one the project is for.')
    return next(iter(users))


def existing_names(projects):
    names = {}
    try:
        for entry in os.scandir(projects):
            names[fold_name(entry.name)] = entry.name
    except OSError as error:
        raise PlaceError(f'The library\'s projects cannot be listed ({error.strerror or error}).') from None
    return names


def check_new(projects, name):
    taken = existing_names(projects)
    if fold_name(name) in taken:
        raise PlaceError(f'The library already has a project named “{taken[fold_name(name)]}”. Splice never changes '
                         'or replaces an existing project: choose a new name.')


def _modes(projects):
    """The mode, owner and group of an existing project folder and its Project.db (else the
    Projects folder's, and a file mode without execute bits)."""
    try:
        for entry in sorted(os.scandir(projects), key=lambda e: e.name):
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.'):
                folder = os.stat(entry.path, follow_symlinks=False)
                try:
                    db = os.stat(os.path.join(entry.path, 'Project.db'), follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(db.st_mode):
                    return folder, db
    except OSError:
        pass
    folder = os.stat(projects)
    return folder, None


def _renameat2():
    if not sys.platform.startswith('linux'):
        return None
    import ctypes
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError, TypeError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int

    def call(old, new):
        result = int(function(-100, os.fsencode(old), -100, os.fsencode(new), 1))
        return result, ctypes.get_errno()
    return call


def move_no_replace(folder, target):
    """Rename ``folder`` to ``target`` without replacing anything (FileExistsError if taken)."""
    call = _renameat2()
    if call is not None:
        result, code = call(folder, target)
        if result == 0:
            return
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError(code, os.strerror(code), str(target))
        if code not in _UNAVAILABLE:
            raise OSError(code, os.strerror(code), str(folder), None, str(target))
    if sys.platform != 'win32' and os.path.lexists(target):
        raise FileExistsError(errno.EEXIST, 'exists', str(target))
    os.rename(folder, target)


def _held(path):
    """Whether a live process holds the ``flock`` of the lock file ``path``: True, False, or None
    where the file system cannot tell (no ``fcntl``, or ``flock`` refused). Raises ``OSError``
    when the file cannot be opened (a link, no permission)."""
    try:
        import fcntl
    except ImportError:
        return None
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _read_lock(path):
    """The build folder a lock names, '' for an empty lock (a save killed before it wrote it),
    or None when the file is not one of Splice's locks."""
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        text = os.read(fd, 256).decode('ascii', 'replace')
    finally:
        os.close(fd)
    if text == '':
        return ''
    if text.startswith(_LOCK_TEXT) and _BUILD.match(text[len(_LOCK_TEXT):]):
        return text[len(_LOCK_TEXT):]
    return None


def _ours(folder):
    """Whether the build folder ``folder`` is Splice's: it holds the marker (a regular file,
    not a link, with Splice's text), or nothing at all."""
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    if not names:
        return True
    try:
        info = os.lstat(os.path.join(folder, MARKER))
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(_MARKER_TEXT):
            return False
        fd = os.open(os.path.join(folder, MARKER), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            return os.read(fd, 64) == _MARKER_TEXT
        finally:
            os.close(fd)
    except OSError:
        return False


def clean_stale(root, *, now=None, age=None):
    """Remove what interrupted saves left in ``<Resolve Projects>/.splice-staging`` (see the module
    notes); returns the names removed. Never raises for an entry it cannot remove."""
    staging = Path(root) / STAGING
    removed = []
    try:
        info = os.lstat(staging)
    except OSError:
        return removed
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or \
            (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
        return removed
    now = time.time() if now is None else now
    try:
        # Build folders are listed before the locks are read: a save makes (and holds) its lock,
        # naming its build folder, before it makes the folder, so a folder listed here whose save
        # is alive is named by a held lock below.
        builds = [e for e in os.scandir(staging) if _BUILD.match(e.name)]
        locks = [e for e in os.scandir(staging) if _LOCK.match(e.name)]
    except OSError:
        return removed
    live = set()
    unlockable = False
    for entry in locks:
        try:
            st = os.lstat(entry.path)
            if not stat.S_ISREG(st.st_mode) or (hasattr(os, 'getuid') and st.st_uid != os.getuid()):
                continue  # a link or another user's file: ignored, the other entries are still cleaned
            named = _read_lock(entry.path)
            held = _held(entry.path)
        except OSError:
            continue  # cannot be opened: not a lock a save of this user holds; ignored
        if named is None:
            continue  # not one of Splice's locks: never removed
        unlockable = unlockable or held is None
        limit = STALE_SECONDS_UNLOCKABLE if held is None else (STALE_SECONDS if age is None else age)
        if held or now - st.st_mtime < limit:
            live.add(named)
            continue
        try:
            os.unlink(entry.path)
            removed.append(entry.name)
        except OSError:
            live.add(named)
    for entry in builds:
        try:
            st = os.lstat(entry.path)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or (hasattr(os, 'getuid') and st.st_uid != os.getuid()) or \
                not _ours(entry.path):
            continue  # a link, another user's folder, or a folder without Splice's marker: left alone
        limit = STALE_SECONDS_UNLOCKABLE if unlockable else (STALE_SECONDS if age is None else age)
        if entry.name in live or now - st.st_mtime < limit:
            continue
        try:
            shutil.rmtree(entry.path)  # removes links inside, never follows them
            removed.append(entry.name)
        except OSError:
            continue
    try:
        os.rmdir(staging)
    except OSError:
        pass
    return removed


class Staging:
    """A build folder in the library's staging folder, holding the project folder under its new
    name, and the lock of the name; removed on every path by :meth:`close`."""

    def __init__(self, library, name, user=None):
        self.name = check_name(name)
        self.library, self.root, users = library_users(library)
        self.user = pick_user(users, user)
        self.projects = users[self.user]
        check_new(self.projects, self.name)
        if not os.access(self.projects, os.W_OK | os.X_OK) or not os.access(self.root, os.W_OK | os.X_OK):
            raise PlaceError('The library is not writable by Splice (no permission to add a project).')
        self.staging = self.root / STAGING
        self.lock = self.work = self.lock_fd = None
        self.placed = None
        self.cleaned = clean_stale(self.root)
        try:
            build = 'build-' + secrets.token_hex(8)
            self._lock(build)
            self.work = self.staging / build
            os.mkdir(self.work, 0o700)
            marker = os.open(self.work / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                os.write(marker, _MARKER_TEXT)
            finally:
                os.close(marker)
            if os.stat(self.work).st_dev != os.stat(self.projects).st_dev:
                raise PlaceError('The staging folder is on another file system than the projects, so the project '
                                 'could not be moved in with one rename.')
            self.folder = self.work / self.name
            os.mkdir(self.folder, 0o700)
        except OSError as error:
            self.close()
            raise PlaceError(f'Splice cannot write in the library\'s staging folder {self.staging} '
                             f'({error.strerror or error}); nothing was placed.') from None
        except BaseException:
            self.close()
            raise

    def _lock(self, build):
        """Make (or use) the staging folder and take the lock of the name: a new file, held with
        ``flock`` for as long as this save runs and naming its build folder (so a later save can
        tell a live save's staging from what a killed one left). Another save that removes the
        empty staging folder meanwhile is answered by making it again."""
        import hashlib
        lock = self.staging / ('lock-' + hashlib.sha256(fold_name(self.name).encode()).hexdigest()[:32])
        for _attempt in range(8):
            try:
                os.mkdir(self.staging, 0o700)
            except FileExistsError:
                _no_link(self.staging, 'the staging folder')
                if hasattr(os, 'getuid') and os.stat(self.staging).st_uid != os.getuid():
                    raise PlaceError('The library\'s staging folder belongs to another user; remove '
                                     f'{self.staging} when no save is running.') from None
            try:
                fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            except FileExistsError:
                raise PlaceError(f'Another save of a project named “{self.name}” is running. Try again when it has '
                                 'finished (what an interrupted save left is removed after '
                                 f'{STALE_SECONDS} seconds).') from None
            except FileNotFoundError:
                continue  # the staging folder was removed by a save that just finished
            self.lock, self.lock_fd = lock, fd
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (ImportError, OSError):
                pass  # no flock here: stale entries are then removed by age only
            os.write(fd, (_LOCK_TEXT + build).encode('ascii'))
            return
        raise PlaceError('The library\'s staging folder keeps disappearing; try the save again.')

    @property
    def db(self):
        return self.folder / 'Project.db'

    def finish(self):
        """Give the project the library's modes and owner, then move it into ``Projects``."""
        for suffix in ('-journal', '-wal', '-shm'):
            if os.path.lexists(str(self.db) + suffix):
                raise PlaceError(f'Project.db{suffix} was left beside the written project; nothing was placed.')
        folder_info, db_info = _modes(self.projects)
        file_mode = stat.S_IMODE(db_info.st_mode) if db_info else stat.S_IMODE(folder_info.st_mode) & 0o666
        os.chmod(self.db, file_mode)
        os.chmod(self.folder, stat.S_IMODE(folder_info.st_mode))
        self.notes = []
        if hasattr(os, 'chown'):
            for path, info in ((self.db, db_info or folder_info), (self.folder, folder_info)):
                current = os.stat(path, follow_symlinks=False)
                if (current.st_uid, current.st_gid) == (info.st_uid, info.st_gid):
                    continue
                try:
                    os.chown(path, info.st_uid, info.st_gid, follow_symlinks=False)
                except OSError:
                    self.notes.append(f'The owner of {path.name} could not be set to match the library\'s '
                                      'projects (not permitted); it keeps Splice\'s.')
        library_users(self.library)  # checked again right before the rename
        check_new(self.projects, self.name)
        target = self.projects / self.name
        try:
            move_no_replace(self.folder, target)
        except FileExistsError:
            raise PlaceError(f'A project named “{self.name}” appeared in the library while saving; nothing was '
                             'replaced. Choose another name.') from None
        except OSError as error:
            raise PlaceError(f'The project could not be moved into the library ({error.strerror or error}).') from None
        self.placed = target
        return target

    def close(self):
        """Remove the build folder, the lock and an empty staging folder; returns what could not
        be removed (named), or []."""
        left = []
        if self.work is not None and os.path.lexists(self.work):
            try:
                shutil.rmtree(self.work)
            except OSError:
                left.append(str(self.work))
        if self.lock is not None:
            try:
                os.unlink(self.lock)
            except FileNotFoundError:
                pass
            except OSError:
                left.append(str(self.lock))
        if self.lock_fd is not None:
            os.close(self.lock_fd)  # releases the flock
            self.lock_fd = None
        try:
            os.rmdir(self.staging)
        except OSError:
            pass
        self.work = self.lock = None
        return left
