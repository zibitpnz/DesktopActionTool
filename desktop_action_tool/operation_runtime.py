"""Shared cancellation, progress counts and held-input cleanup."""
from contextlib import contextmanager
import time
import sys
from .action_runtime import ActionAborted, ActionError

CURRENT_OPERATION = None

def current_operation():
    return CURRENT_OPERATION


def check_cancelled() -> None:
    if CURRENT_OPERATION is not None:
        CURRENT_OPERATION.check()


def interruptible_sleep(seconds: float) -> None:
    if CURRENT_OPERATION is None:
        time.sleep(seconds)
    else:
        CURRENT_OPERATION.wait(seconds)


def count_completed(name: str, amount: int = 1) -> None:
    if CURRENT_OPERATION is not None:
        CURRENT_OPERATION.count(name, amount)


@contextmanager
def operation_session(operation):
    from .input_backend import send_input
    global CURRENT_OPERATION
    previous = CURRENT_OPERATION
    CURRENT_OPERATION = operation
    try:
        operation.check()
        yield operation
    finally:
        original_error = sys.exc_info()[1]
        errors = []
        try:
            for identity, release in reversed(list(operation.held.items())):
                try:
                    send_input(release)
                except (Exception, KeyboardInterrupt) as exc:
                    errors.append({"input": str(identity), "error": str(exc)})
        finally:
            CURRENT_OPERATION = previous
        if errors:
            failure = ActionError("RELEASE_FAILED", "could not release all held inputs", "check held keys/buttons before another action")
            failure.release_errors = errors
            failure.aborted = isinstance(original_error, (ActionAborted, KeyboardInterrupt))
            raise failure from original_error
