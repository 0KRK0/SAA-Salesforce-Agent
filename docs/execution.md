# Durable execution

The promise is narrow and testable:

> **Closing a tab does not stop a change to your Salesforce org, and reopening
> one does not start a second.**

Everything in `app/execution/` exists to make that true.

## The inversion

Before: an HTTP request created a run and streamed events from an in-memory
queue. The run *was* the request. Close the tab, and a half-finished change to
a customer's org was abandoned with no record of where it stopped.

Now:

```
POST /conversations/{id}/messages   →  202 { run_id }      (creates, returns)
        │
        ▼
   agent_runs (QUEUED)  ──claim──▶  RunWorker  ──▶  run_events (durable)
        │                              │                    │
        │                          heartbeat                │
        ▼                                                   ▼
GET /runs/{id}/events?after=N  ◀────── reads the log ───────┘
```

The SSE connection is a **view onto a log**, not the thing producing it. That
one change is what makes every property below possible.

## Claiming

A run belongs to exactly one worker. The claim is an atomic conditional
`UPDATE ... WHERE claimed_by IS NULL`, so two workers racing produce one winner
and one no-op — on SQLite and Postgres alike, with no lock table. Every API
process runs a worker by default; several processes can share a queue with no
coordination beyond the database.

## Heartbeats and reclaiming

A crashed worker cannot release its own claim — that is the one thing a crashed
process cannot do. So ownership expires instead: a claim whose heartbeat has
gone stale (`RUN_HEARTBEAT_TIMEOUT_SECONDS`) is reclaimable, and the run returns
to `QUEUED`. It is **not** marked failed: nothing is known to have gone wrong
with the *work*, only with the process that was doing it.

A run `WAITING_FOR_APPROVAL` is never reclaimed. A person taking an hour to
approve something is not a stalled worker, and the worker releases its slot
while they decide.

## The state machine

`app/execution/state.py` holds the legal transitions as a table, and every move
goes through `transition()`. Two rules matter most:

- **A terminal run never leaves.** A `COMPLETED` run that could return to
  `EXECUTING` would let one approval authorize a second change.
- **`WAITING_FOR_APPROVAL` cannot jump to `COMPLETED`.** That would claim
  success for work the approval authorized and nothing ever executed.

An illegal transition raises. It is a bug in the runtime, not a condition to
recover from.

## Cancellation is cooperative

`POST /runs/{id}/cancel` sets a flag. A claimed run stops at its **next step
boundary**, never mid-call.

That is deliberate. Killing a worker during a Salesforce write would leave the
org changed and this system unable to say whether it was — the one state it
must never produce. A run that has not been claimed yet is cancelled outright,
because nothing is in flight.

A cancelled run records what it had already done, in its timeline and as an
assistant message. Someone who cancels still needs to know exactly what changed
before the stop took effect.

## Deadlines

Every run gets a deadline from `MAX_EXECUTION_SECONDS`. An overrun run is marked
`EXPIRED`, not `FAILED`: "we stopped waiting" is a different statement from "the
work went wrong", and an operator reading the audit trail has to be able to tell
them apart.

## The timeline

Every event is written to `run_events` *before* it reaches a browser, with a
per-run `sequence` allocated by the worker that owns the run.

- `GET /runs/{id}/events?after=N` — SSE. Replays from `N`, then follows.
- `GET /runs/{id}/timeline?after=N` — the same data without a stream, for a
  client that would rather poll than hold a connection.
- Each SSE frame carries `id: <sequence>`, so a browser's automatic reconnect
  sends `Last-Event-ID` and resumes exactly, with no client-side bookkeeping.

Event payloads go through the same redaction as audit rows, and a single event
is capped at 64 KB — a tool returning a huge payload must not be able to make
its own timeline unreadable, or the table unbounded. An oversized event is
truncated with a note pointing at the full record in the tool-execution history,
rather than dropped.

A stream on a run that is finished, or waiting for a human, replays its history
and closes rather than holding a connection open for nothing.

## Finding live work

`GET /conversations/{id}/runs` reports which runs are still active. A returning
client calls it when opening a conversation and picks the stream back up — which
is how a reloaded page shows a run that has been going for ten minutes rather
than an empty box.

## Shutdown

`RunWorker.stop()` stops claiming new work and lets in-flight runs finish. They
are not cancelled: a run interrupted between a Salesforce write and its
verification is precisely the state this system cannot describe honestly, so
shutdown waits rather than creating one.

## Configuration

| Setting | Meaning |
| --- | --- |
| `RUN_WORKER_ENABLED` | Run a worker in this process. Off means queued runs wait for a separate worker. |
| `RUN_WORKER_CONCURRENCY` | Runs executed at once per process. |
| `RUN_HEARTBEAT_TIMEOUT_SECONDS` | How long a silent claim survives before it is reclaimable. |
| `MAX_EXECUTION_SECONDS` | Deadline ceiling. A project policy may be stricter. |
| `WORKER_ID` | Identity in claims and logs. Defaults to `host:pid`. |
