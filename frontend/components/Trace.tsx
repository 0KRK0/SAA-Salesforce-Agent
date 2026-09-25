"use client";

import type { TraceEntry } from "@/lib/types";

/**
 * The execution trace shows what the agent DID (tools, Salesforce calls,
 * approvals, deployments) — never internal reasoning.
 */
export function Trace({ entries }: { entries: TraceEntry[] }) {
  if (!entries.length) return null;
  return (
    <div className="trace">
      {entries.map((e) => (
        <div className="trace-row" key={e.id}>
          <span className={`dot ${e.status ?? ""}`} />
          <span className="label">{e.label}</span>
          {e.risk && <span className={`badge ${e.risk}`}>{e.risk}</span>}
          {e.detail && <span className="detail">{e.detail}</span>}
        </div>
      ))}
    </div>
  );
}
