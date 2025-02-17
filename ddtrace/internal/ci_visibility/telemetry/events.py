from enum import Enum
from typing import List
from typing import Optional
from typing import Tuple

from ddtrace.internal.ci_visibility.telemetry.constants import CIVISIBILITY_TELEMETRY_NAMESPACE as _NAMESPACE
from ddtrace.internal.ci_visibility.telemetry.constants import EVENT_TYPES
from ddtrace.internal.ci_visibility.telemetry.constants import TEST_FRAMEWORKS
from ddtrace.internal.logger import get_logger
from ddtrace.internal.telemetry import telemetry_writer


log = get_logger(__name__)


class EVENTS_TELEMETRY(str, Enum):
    CREATED = "event_created"
    FINISHED = "event_finished"
    MANUAL_API_EVENT = "manual_api_event"
    ENQUEUED_FOR_SERIALIZATION = "events_enqueued_for_serialization"


def _record_event(
    event: EVENTS_TELEMETRY,
    event_type: EVENT_TYPES,
    test_framework: Optional[TEST_FRAMEWORKS],
    has_codeowners: Optional[bool] = False,
    is_unsupported_ci: Optional[bool] = False,
    is_benchmark: Optional[bool] = False,
    is_new: Optional[bool] = False,
    is_retry: Optional[bool] = False,
    early_flake_detection_abort_reason: Optional[str] = None,
):
    if has_codeowners and event_type != EVENT_TYPES.SESSION:
        log.debug("has_codeowners tag can only be set for sessions, but event type is %s", event_type)
    if is_unsupported_ci and event_type != EVENT_TYPES.SESSION:
        log.debug("unsupported_ci tag can only be set for sessions, but event type is %s", event_type)
    if is_benchmark and event_type != EVENT_TYPES.TEST:
        log.debug("is_benchmark tag can only be set for tests, but event type is %s", event_type)
    if is_new and not (event_type == EVENT_TYPES.TEST and event == EVENTS_TELEMETRY.FINISHED):
        log.debug(
            "is_new tag can only be set for test finishes, but event type is %s and event is %s", event_type, event
        )
    if is_retry and not (event_type == EVENT_TYPES.TEST and event == EVENTS_TELEMETRY.FINISHED):
        log.debug(
            "is_retry tag can only be set for test finishes, but event type is %s and event is %s", event_type, event
        )
    if early_flake_detection_abort_reason and (
        event_type not in [EVENT_TYPES.TEST, EVENT_TYPES.SESSION] or event != EVENTS_TELEMETRY.FINISHED
    ):
        log.debug(
            "early_flake_detection_abort_reason tag can only be set for tests and session finish events",
        )

    _tags: List[Tuple[str, str]] = [("event_type", event_type)]
    if test_framework and test_framework != TEST_FRAMEWORKS.MANUAL:
        _tags.append(("test_framework", test_framework))
    if event_type == EVENT_TYPES.SESSION:
        _tags.append(("has_codeowners", "1" if has_codeowners else "0"))
        _tags.append(("is_unsupported_ci", "1" if has_codeowners else "0"))

    if event_type == EVENT_TYPES.TEST:
        _tags.append(("is_benchmark", "1" if is_benchmark else "0"))
        if event == EVENTS_TELEMETRY.FINISHED:
            if is_new:
                _tags.append(("is_new", "true"))
            if is_retry:
                _tags.append(("is_retry", "true"))

    if (
        early_flake_detection_abort_reason
        and event == EVENTS_TELEMETRY.FINISHED
        and event_type in [EVENT_TYPES.TEST, EVENT_TYPES.SESSION]
    ):
        _tags.append(("early_flake_detection_abort_reason", early_flake_detection_abort_reason))

    telemetry_writer.add_count_metric(_NAMESPACE, event, 1, tuple(_tags))


def record_event_created(
    event_type: EVENT_TYPES,
    test_framework: TEST_FRAMEWORKS,
    has_codeowners: Optional[bool] = None,
    is_unsupported_ci: Optional[bool] = None,
    is_benchmark: Optional[bool] = False,
):
    if test_framework == TEST_FRAMEWORKS.MANUAL:
        # manual API usage is tracked only by way of tracking created events
        record_manual_api_event_created(event_type)

    _record_event(
        event=EVENTS_TELEMETRY.CREATED,
        event_type=event_type,
        test_framework=test_framework,
        has_codeowners=has_codeowners,
        is_unsupported_ci=is_unsupported_ci,
        is_benchmark=is_benchmark,
    )


def record_event_finished(
    event_type: EVENT_TYPES,
    test_framework: Optional[TEST_FRAMEWORKS],
    has_codeowners: bool = False,
    is_unsupported_ci: bool = False,
    is_benchmark: bool = False,
    is_new: bool = False,
    is_retry: bool = False,
    early_flake_detection_abort_reason: Optional[str] = None,
):
    _record_event(
        event=EVENTS_TELEMETRY.FINISHED,
        event_type=event_type,
        test_framework=test_framework,
        has_codeowners=has_codeowners,
        is_unsupported_ci=is_unsupported_ci,
        is_benchmark=is_benchmark,
        is_new=is_new,
        is_retry=is_retry,
        early_flake_detection_abort_reason=early_flake_detection_abort_reason,
    )


def record_manual_api_event_created(event_type: EVENT_TYPES):
    # Note: _created suffix is added in cases we were to change the metric name in the future.
    # The current metric applies to event creation even though it does not specify it
    telemetry_writer.add_count_metric(_NAMESPACE, EVENTS_TELEMETRY.MANUAL_API_EVENT, 1, (("event_type", event_type),))


def record_events_enqueued_for_serialization(events_count: int):
    telemetry_writer.add_count_metric(_NAMESPACE, EVENTS_TELEMETRY.ENQUEUED_FOR_SERIALIZATION, events_count)
