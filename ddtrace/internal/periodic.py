# -*- encoding: utf-8 -*-
import atexit
import typing  # noqa:F401

from ddtrace.internal import forksafe
from ddtrace.internal import service
from ddtrace.internal._threads import PeriodicThread
from ddtrace.internal._threads import periodic_threads
from ddtrace.internal.compat import dataclasses


@atexit.register
def _():
    # If the interpreter is shutting down we need to make sure that the threads
    # are stopped before the runtime is marked as finalising. This is because
    # any attempt to acquire the GIL while the runtime is finalising will cause
    # the acquiring thread to be terminated with pthread_exit (on Linux). This
    # causes a SIGABRT with GCC that cannot be caught, so we need to avoid
    # getting to that stage.
    for thread in periodic_threads.values():
        thread._atexit()


@forksafe.register
def _():
    # No threads are running after a fork so we clean up the periodic threads
    for thread in periodic_threads.values():
        thread._after_fork()
    periodic_threads.clear()


@dataclasses.dataclass(eq=False)
class PeriodicService(service.Service):
    """A service that runs periodically."""

    _interval: float = dataclasses.field(default=10.0, init=False, repr=False)
    _worker: typing.Optional[PeriodicThread] = dataclasses.field(default=None, init=False, repr=False)
    interval: float = 10

    @property  # type: ignore[no-redef]
    def interval(self) -> float:
        return self._interval

    @interval.setter
    def interval(
        self,
        value: float,
    ):
        self._interval = value
        # Update the interval of the PeriodicThread based on ours
        if self._worker:
            self._worker.interval = value  # type: ignore[attr-defined]

    def _start_service(self, *args, **kwargs):
        # type: (typing.Any, typing.Any) -> None
        """Start the periodic service."""
        self._worker = PeriodicThread(
            interval=self._interval,
            target=self.periodic,
            name="%s:%s" % (self.__class__.__module__, self.__class__.__name__),
            on_shutdown=self.on_shutdown,
        )
        self._worker.start()

    def _stop_service(self, *args, **kwargs):
        # type: (typing.Any, typing.Any) -> None
        """Stop the periodic collector."""
        if self._worker is not None:
            self._worker.stop()
        super(PeriodicService, self)._stop_service(*args, **kwargs)

    def join(
        self,
        timeout: typing.Optional[float] = None,
    ):
        if self._worker:
            self._worker.join(timeout)

    @staticmethod
    def on_shutdown():
        pass

    def periodic(self):
        # type: (...) -> None
        pass


class AwakeablePeriodicService(PeriodicService):
    """A service that runs periodically but that can also be awakened on demand."""

    def awake(self):
        # type: (...) -> None
        if self._worker is not None:
            self._worker.awake()
