"""The run state machine, as a table rather than as scattered assignments.

A run's state is owned by the server. It is never derived from whether a
browser is connected, because "the user closed the tab" is not a fact about the
work — the change to Salesforce either happened or it did not.

Every transition goes through `transition()`, which refuses illegal moves. That
is what stops the two failure modes that matter: a finished run being reopened,
and a run being marked COMPLETED from a state where nothing verified it.
"""

from __future__ import annotations

from app.models import TERMINAL_RUN_STATES, RunState

#: Legal moves. Anything not listed here is a bug, not a possibility.
TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.QUEUED, RunState.CANCELLED, RunState.FAILED}),
    RunState.QUEUED: frozenset(
        {
            RunState.PLANNING,
            RunState.INSPECTING,
            RunState.CANCELLED,
            RunState.FAILED,
            RunState.EXPIRED,
        }
    ),
    RunState.PLANNING: frozenset(
        {
            RunState.INSPECTING,
            RunState.EXECUTING,
            RunState.WAITING_FOR_APPROVAL,
            RunState.VERIFYING,
            RunState.COMPLETED,
            RunState.CANCELLED,
            RunState.FAILED,
            RunState.EXPIRED,
        }
    ),
    RunState.INSPECTING: frozenset(
        {
            RunState.PLANNING,
            RunState.EXECUTING,
            RunState.WAITING_FOR_APPROVAL,
            RunState.VERIFYING,
            RunState.COMPLETED,
            RunState.CANCELLED,
            RunState.FAILED,
            RunState.EXPIRED,
        }
    ),
    RunState.EXECUTING: frozenset(
        {
            RunState.PLANNING,
            RunState.INSPECTING,
            RunState.WAITING_FOR_APPROVAL,
            RunState.VERIFYING,
            RunState.COMPLETED,
            RunState.CANCELLED,
            RunState.FAILED,
            RunState.EXPIRED,
        }
    ),
    #: A run waiting on a human can be resumed, cancelled, or time out. It
    #: cannot go straight to COMPLETED — that would mean skipping the work the
    #: approval authorized.
    RunState.WAITING_FOR_APPROVAL: frozenset(
        {
            RunState.QUEUED,
            RunState.PLANNING,
            RunState.EXECUTING,
            RunState.CANCELLED,
            RunState.FAILED,
            RunState.EXPIRED,
        }
    ),
    RunState.VERIFYING: frozenset(
        {
            RunState.PLANNING,
            RunState.EXECUTING,
            RunState.COMPLETED,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    # Terminal. Nothing leaves.
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.EXPIRED: frozenset(),
}


class IllegalTransition(RuntimeError):
    """An attempt to move a run somewhere the state machine does not allow."""

    def __init__(self, current: RunState, target: RunState):
        super().__init__(
            f"A run cannot move from {current.value} to {target.value}."
        )
        self.current = current
        self.target = target


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_RUN_STATES


def can_transition(current: RunState, target: RunState) -> bool:
    if current is target:
        # Re-asserting the current state is a no-op, not an error: the runtime
        # sets PLANNING on every step and should not have to remember whether
        # it was already there.
        return True
    return target in TRANSITIONS.get(current, frozenset())


def transition(current: RunState, target: RunState) -> RunState:
    """Return the new state, or raise. Callers assign the return value."""
    if not can_transition(current, target):
        raise IllegalTransition(current, target)
    return target


def describe(state: RunState) -> str:
    """One line a person can read, for the run timeline."""
    return {
        RunState.CREATED: "Created",
        RunState.QUEUED: "Queued — waiting for a worker",
        RunState.PLANNING: "Planning the change",
        RunState.INSPECTING: "Inspecting the org",
        RunState.WAITING_FOR_APPROVAL: "Waiting for approval",
        RunState.EXECUTING: "Making changes",
        RunState.VERIFYING: "Verifying against the org",
        RunState.COMPLETED: "Completed",
        RunState.FAILED: "Failed",
        RunState.CANCELLED: "Cancelled",
        RunState.EXPIRED: "Expired without finishing",
    }.get(state, state.value)
