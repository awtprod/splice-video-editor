"""Bounds on the work one interchange request may do, and a way to stop it early.

An import walks a timeline whose size is not the file's size: one compound clip used ten times,
each using another ten times, multiplies the work at every level. :class:`Budget` counts the
elements visited and the time spent and raises :class:`BudgetExceeded` (a ``ValueError``, so the
server answers 400) when either runs out or when the request was cancelled through
:func:`cancel`. Exports use the same object to bound source probing.
"""
from __future__ import annotations

import re
import threading
import time

IMPORT_SECONDS = 20.0
IMPORT_VISITS = 250_000
_JOB_ID = re.compile(r'[A-Za-z0-9_-]{8,64}')
_active: dict = {}
_lock = threading.Lock()


class BudgetExceeded(ValueError):
    pass


class Budget:
    def __init__(self, seconds: float = IMPORT_SECONDS, visits: int = IMPORT_VISITS, job: str | None = None,
                 what: str = 'The import'):
        self.deadline = time.monotonic() + seconds
        self.seconds = seconds
        self.visits_left = visits
        self.visits = visits
        self.what = what
        self.cancelled = threading.Event()
        self.job = job if isinstance(job, str) and _JOB_ID.fullmatch(job) else None

    def __enter__(self):
        if self.job:
            with _lock:
                _active[self.job] = self
        return self

    def __exit__(self, *_exc):
        if self.job:
            with _lock:
                if _active.get(self.job) is self:
                    del _active[self.job]
        return False

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check(self):
        if self.cancelled.is_set():
            raise BudgetExceeded(f'{self.what} was cancelled.')
        if time.monotonic() > self.deadline:
            raise BudgetExceeded(f'{self.what} took longer than {self.seconds:g} seconds and was stopped.')

    def tick(self, count: int = 1):
        self.visits_left -= count
        if self.visits_left < 0:
            raise BudgetExceeded(f'{self.what} stopped after visiting {self.visits} timeline items: compound clips are '
                                 'used so many times that the timeline expands far beyond the file. Flatten them first.')
        if self.visits_left % 256 == 0:
            self.check()


def cancel(job) -> bool:
    """Ask the running request ``job`` to stop; False when no such request is running."""
    with _lock:
        budget = _active.get(job) if isinstance(job, str) else None
    if budget is None:
        return False
    budget.cancelled.set()
    return True
