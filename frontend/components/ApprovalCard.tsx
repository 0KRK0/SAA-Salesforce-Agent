"use client";

import { useEffect, useState } from "react";
import type { Approval, ComponentDiff, PlanFinding } from "@/lib/types";

interface Props {
  approval: Approval;
  onDecide: (
    id: string,
    decision: "approve" | "reject",
    note?: string,
    modified?: Record<string, unknown>,
  ) => Promise<void>;
}

/**
 * The approval card is the product's whole safety story made visible. Whoever
 * reads it has to be able to answer three questions without leaving the page:
 * what exactly changes, what else in the org is involved, and what happens if
 * it is wrong. Everything rendered here serves one of those.
 */
export function ApprovalCard({ approval, onDecide }: Props) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() =>
    JSON.stringify(approval.arguments ?? {}, null, 2),
  );
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [remaining, setRemaining] = useState<string>("");

  const plan = approval.plan ?? {};
  const decided = approval.state !== "PENDING";
  const required = approval.approvals_required ?? 1;
  const recorded = approval.approvals_recorded ?? 0;

  // An approval that ages out authorizes nothing, so the countdown is part of
  // the decision, not decoration.
  useEffect(() => {
    if (!approval.expires_at || decided) return;
    const tick = () => {
      const ms = new Date(approval.expires_at as string).getTime() - Date.now();
      if (ms <= 0) {
        setRemaining("expired");
        return;
      }
      const minutes = Math.floor(ms / 60000);
      setRemaining(
        minutes >= 60
          ? `${Math.floor(minutes / 60)}h ${minutes % 60}m left`
          : `${minutes}m left`,
      );
    };
    tick();
    const id = setInterval(tick, 30_000);
    return () => clearInterval(id);
  }, [approval.expires_at, decided]);

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    setError(null);
    try {
      let modified: Record<string, unknown> | undefined;
      if (editing && decision === "approve") {
        modified = JSON.parse(draft) as Record<string, unknown>;
      }
      await onDecide(approval.id, decision, note || undefined, modified);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Decision failed");
    } finally {
      setBusy(false);
    }
  }

  const expired = approval.expired || remaining === "expired";

  return (
    <div className="approval">
      <header>
        <strong>{plan.title ?? `Approve ${approval.tool_name}`}</strong>
        <span className="row-between" style={{ gap: 6 }}>
          {approval.expires_at && !decided && (
            <span className={`badge ${expired ? "HIGH" : ""}`}>{remaining}</span>
          )}
          <span className={`badge ${approval.risk_level}`}>
            {approval.risk_level} RISK
          </span>
        </span>
      </header>

      <div className="body">
        {plan.summary && <p style={{ marginTop: 0 }}>{plan.summary}</p>}

        {required > 1 && (
          <div className="banner">
            This change needs {required} approvals from{" "}
            {(approval.eligible_roles ?? []).join(", ") || "an authorized role"}.{" "}
            {recorded} of {required} recorded so far.
          </div>
        )}
        {approval.require_separate_approver && (
          <div className="banner">
            Separation of duties: the person who requested this change cannot
            approve it.
          </div>
        )}
        {expired && !decided && (
          <div className="banner error">
            This approval has expired. Nothing will execute — ask the agent to
            propose the change again so it is re-checked against the org.
          </div>
        )}

        {!!plan.details?.length && (
          <table>
            <tbody>
              {plan.details.map((d, i) => (
                <tr key={i}>
                  <td>{d.field}</td>
                  <td>
                    {d.old_value !== undefined && d.old_value !== null && (
                      <>
                        <span className="old">{format(d.old_value)}</span>{" "}
                      </>
                    )}
                    <span className="new">{format(d.new_value)}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <Findings findings={plan.findings} />

        {!!plan.grants?.length && (
          <div className="stack" style={{ gap: 4 }}>
            <strong style={{ fontSize: 13 }}>This grants:</strong>
            {plan.grants.map((g, i) => (
              <div key={i} className="finding">
                <span className={`badge ${severityClass(g.severity)}`}>
                  {g.severity}
                </span>{" "}
                <strong>{g.permission}</strong> — {g.why}
              </div>
            ))}
          </div>
        )}

        <Diffs diffs={plan.diff} />
        <Code code={plan.code} />

        {!!plan.existing_automation?.length && (
          <details>
            <summary>
              Automation already running on this object (
              {plan.existing_automation.length})
            </summary>
            <ul className="muted" style={{ fontSize: 12 }}>
              {plan.existing_automation.map((f, i) => (
                <li key={i}>{JSON.stringify(f)}</li>
              ))}
            </ul>
          </details>
        )}

        {plan.impact && (
          <p className="muted" style={{ fontSize: 13 }}>
            <strong>Impact:</strong> {plan.impact}
          </p>
        )}
        {!!plan.rollback_caveats?.length && (
          <div className="banner">
            <strong>Rollback caveats:</strong> {plan.rollback_caveats.join(" ")}
          </div>
        )}
        {!!plan.notes?.length && (
          <p className="muted" style={{ fontSize: 12 }}>
            {plan.notes.join(" ")}
          </p>
        )}
        {!!plan.risk?.reasons?.length && (
          <p className="muted" style={{ fontSize: 12 }}>
            {plan.risk.reasons.join(" ")}
          </p>
        )}

        {editing && (
          <>
            <p className="muted" style={{ fontSize: 12 }}>
              Editing the arguments invalidates any approvals already recorded —
              the change everyone reviewed is no longer the change being made.
            </p>
            <textarea
              rows={10}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              style={{ fontFamily: "var(--mono)", fontSize: 12 }}
            />
          </>
        )}
        {!decided && (
          <input
            placeholder="Note (recorded in the audit log)"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
        )}
        {error && <div className="banner error">{error}</div>}

        {/*
          Why this approval is not moving. A control that stops and says nothing
          is indistinguishable from a control that is broken — and the specific
          case that looked broken was a change needing two approvals in a
          project with one person in it.
        */}
        {!decided && approval.deadlocked && (
          <div className="banner error">
            <strong>This approval cannot be completed as configured.</strong>
            <div style={{ marginTop: 6, fontSize: 13 }}>
              {approval.deadlock_detail}
            </div>
          </div>
        )}
        {!decided &&
          !approval.deadlocked &&
          approval.you_can_decide === false &&
          approval.you_cannot_decide_because && (
            <div className="banner">
              {approval.you_cannot_decide_because}
              {approval.outstanding_approvals ? (
                <>
                  {" "}
                  Still waiting on {approval.outstanding_approvals} more from{" "}
                  {(approval.eligible_roles ?? []).join(", ") || "an eligible approver"}.
                </>
              ) : null}
            </div>
          )}

        {!!approval.decisions?.length && (
          <div className="muted" style={{ fontSize: 12 }}>
            {approval.decisions.map((d, i) => (
              <div key={i}>
                {d.decision === "approve" ? "Approved" : "Rejected"} by {d.role} ·{" "}
                {new Date(d.at).toLocaleString()}
                {d.note ? ` — ${d.note}` : ""}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="actions">
        {decided ? (
          <span className="muted">
            {approval.state === "APPROVED"
              ? "Approved"
              : approval.state === "EXPIRED"
                ? `Invalidated${approval.invalidated_reason ? `: ${approval.invalidated_reason}` : ""}`
                : "Rejected"}{" "}
            — this decision is recorded in the audit log.
          </span>
        ) : (
          <>
            <button
              className="primary"
              // The server owns eligibility, so ask it rather than guessing.
              // Offering a button the API will refuse — which is what
              // "Approve (2/2)" did to somebody who had already voted — reads
              // as a broken product rather than as a control doing its job.
              disabled={busy || expired || approval.you_can_decide === false}
              onClick={() => decide("approve")}
            >
              {approval.you_have_decided
                ? `You approved · ${recorded}/${required}`
                : required > 1
                  ? `Approve (${recorded + 1}/${required})`
                  : "Approve"}
            </button>
            <button className="danger" disabled={busy} onClick={() => decide("reject")}>
              Reject
            </button>
            <button
              className="ghost"
              disabled={busy}
              onClick={() => setEditing((v) => !v)}
            >
              {editing ? "Cancel edit" : "Modify"}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function Findings({ findings }: { findings?: PlanFinding[] }) {
  if (!findings?.length) return null;
  return (
    <div className="stack" style={{ gap: 4 }}>
      <strong style={{ fontSize: 13 }}>Findings</strong>
      {findings.map((f, i) => (
        <div key={i} className="finding">
          <span className={`badge ${severityClass(f.severity)}`}>
            {f.severity ?? "info"}
          </span>{" "}
          {f.unit ? <code>{f.unit}</code> : null} {f.message}
        </div>
      ))}
    </div>
  );
}

function Diffs({ diffs }: { diffs?: ComponentDiff[] }) {
  const changed = (diffs ?? []).filter((d) => d.status !== "unchanged");
  if (!changed.length) return null;
  return (
    <details open={changed.length <= 3}>
      <summary>
        {changed.length} component{changed.length === 1 ? "" : "s"} change
      </summary>
      {changed.map((d) => (
        <div key={d.path} className="diff-block">
          <div className="row-between">
            <code>{d.path}</code>
            <span className="muted" style={{ fontSize: 12 }}>
              {d.status === "new"
                ? "new"
                : `+${d.added_lines} / −${d.removed_lines}`}
            </span>
          </div>
          {d.diff && (
            <pre className="diff">
              {d.diff.split("\n").map((line, i) => (
                <span key={i} className={diffLineClass(line)}>
                  {line}
                  {"\n"}
                </span>
              ))}
            </pre>
          )}
        </div>
      ))}
    </details>
  );
}

function Code({ code }: { code?: Record<string, string> }) {
  const entries = Object.entries(code ?? {});
  if (!entries.length) return null;
  return (
    <details>
      <summary>Source ({entries.length} file{entries.length === 1 ? "" : "s"})</summary>
      {entries.map(([name, body]) => (
        <div key={name} className="diff-block">
          <code>{name}</code>
          <pre className="diff">{body}</pre>
        </div>
      ))}
    </details>
  );
}

function diffLineClass(line: string): string {
  if (line.startsWith("+") && !line.startsWith("+++")) return "add";
  if (line.startsWith("-") && !line.startsWith("---")) return "del";
  if (line.startsWith("@@")) return "hunk";
  return "";
}

function severityClass(severity?: string): string {
  switch ((severity ?? "").toLowerCase()) {
    case "critical":
    case "high":
      return "HIGH";
    case "warning":
    case "medium":
      return "MEDIUM";
    default:
      return "LOW";
  }
}

function format(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
