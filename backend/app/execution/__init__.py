"""Durable, server-owned execution.

A run belongs to the server, not to a browser connection. The request that
accepts work creates a run and returns; a worker executes it and writes every
event to a durable timeline; a client reads that timeline and can disconnect
and reconnect at any point without losing or repeating anything.

Closing a tab does not stop a change to a customer's Salesforce org, and
reopening one does not start a second.
"""

from app.execution.queue import (
    cancel_requested,
    claim,
    claim_next,
    enqueue,
    expire_overdue,
    heartbeat,
    reclaim_stale,
    request_cancel,
)
from app.execution.state import IllegalTransition, can_transition, is_terminal, transition
from app.execution.worker import RunWorker

__all__ = [
    "IllegalTransition",
    "RunWorker",
    "can_transition",
    "cancel_requested",
    "claim",
    "claim_next",
    "enqueue",
    "expire_overdue",
    "heartbeat",
    "is_terminal",
    "reclaim_stale",
    "request_cancel",
    "transition",
]
