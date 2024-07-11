from __future__ import absolute_import

import threading
import typing  # noqa:F401

import attr

from ddtrace.internal.compat import dataclasses

from . import _lock


@dataclasses.dataclass(slots=True)
class ThreadingLockAcquireEvent(_lock.LockAcquireEvent):
    """A threading.Lock has been acquired."""


@dataclasses.dataclass(slots=True)
class ThreadingLockReleaseEvent(_lock.LockReleaseEvent):
    """A threading.Lock has been released."""


class _ProfiledThreadingLock(_lock._ProfiledLock):
    ACQUIRE_EVENT_CLASS = ThreadingLockAcquireEvent
    RELEASE_EVENT_CLASS = ThreadingLockReleaseEvent


@attr.s
class ThreadingLockCollector(_lock.LockCollector):
    """Record threading.Lock usage."""

    PROFILED_LOCK_CLASS = _ProfiledThreadingLock

    def _get_original(self):
        # type: (...) -> typing.Any
        return threading.Lock

    def _set_original(
        self, value  # type: typing.Any
    ):
        # type: (...) -> None
        threading.Lock = value  # type: ignore[misc]
