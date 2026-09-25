"use client";

import { useEffect, useState } from "react";
import { Shell } from "@/components/Shell";
import { api } from "@/lib/api";
import type { ChangeSetSummary, ComponentDiff, DataJobSummary } from "@/lib/types";

/**
 * Change sets and bulk jobs — the two kinds of work whose outcome someone will
 * need to look up afterwards, including whether it can still be rolled back.
 */
export default function ReleasesPage() {
  const [changeSets, setChangeSets] = useState<ChangeSetSummary[]>([]);
  const [jobs, setJobs] = useState<DataJobSummary[]>([]);
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.changeSets(), api.dataJobs()])
      .then(([cs, dj]) => {
        setChangeSets(cs.change_sets);
        setJobs(dj.jobs);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load"));
  }, []);

  const diff = (selected?.diff as ComponentDiff[] | undefined) ?? [];
  const rollback = (selected?.rollback ?? {}) as Record<string, unknown>;

  return (
    <Shell active="releases" topbar={<strong>Releases</strong>}>
      <div className="content">
        <div className="page stack">
          {error && <div className="banner error">{error}</div>}

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Change sets</h3>
            {!changeSets.length && (
              <p className="muted" style={{ fontSize: 13 }}>
                No change sets yet. The agent creates one when a change spans more than
                a single component, or when it should be reversible.
              </p>
            )}
            {!!changeSets.length && (
              <table className="table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>State</th>
                    <th>Components</th>
                    <th>Tests</th>
                    <th>Verified</th>
                    <th>Rollback</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {changeSets.map((c) => (
                    <tr key={c.id}>
                      <td>
                        {c.name}
                        <div className="muted" style={{ fontSize: 12 }}>
                          {new Date(c.created_at).toLocaleString()}
                        </div>
                      </td>
                      <td>
                        <span className={`badge ${stateClass(c.state)}`}>{c.state}</span>
                      </td>
                      <td>{c.components}</td>
                      <td>{c.test_level}</td>
                      <td>{c.verified ? "yes" : "—"}</td>
                      <td>{c.rollback_available ? "available" : "—"}</td>
                      <td>
                        <button
                          className="ghost"
                          onClick={async () => setSelected(await api.changeSet(c.id))}
                        >
                          Diff
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {selected && (
            <div className="card">
              <div className="row-between">
                <h3 style={{ margin: 0 }}>{String(selected.name)}</h3>
                <button className="ghost" onClick={() => setSelected(null)}>
                  Close
                </button>
              </div>
              {!!rollback.summary && (
                <p className="muted" style={{ fontSize: 13 }}>
                  <strong>Rollback:</strong> {String(rollback.summary)}
                </p>
              )}
              {Array.isArray(rollback.caveats) && rollback.caveats.length > 0 && (
                <div className="banner">{(rollback.caveats as string[]).join(" ")}</div>
              )}
              {!diff.length && (
                <p className="muted" style={{ fontSize: 13 }}>
                  No component differences were recorded. Validate the change set to
                  produce a diff.
                </p>
              )}
              {diff.map((d) => (
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
            </div>
          )}

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Bulk data jobs</h3>
            {!jobs.length && (
              <p className="muted" style={{ fontSize: 13 }}>
                No bulk jobs have run. Large data changes go through the Bulk API and
                appear here with the counts Salesforce reported.
              </p>
            )}
            {!!jobs.length && (
              <table className="table">
                <thead>
                  <tr>
                    <th>Job</th>
                    <th>Object</th>
                    <th>State</th>
                    <th>Processed</th>
                    <th>Failed</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((j) => (
                    <tr key={j.id}>
                      <td>
                        {j.operation}
                        <div className="muted" style={{ fontSize: 12 }}>
                          {j.salesforce_job_id ?? j.id}
                        </div>
                      </td>
                      <td>{j.object}</td>
                      <td>{j.state}</td>
                      <td>{j.records_processed.toLocaleString()}</td>
                      <td className={j.records_failed ? "old" : ""}>
                        {j.records_failed.toLocaleString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>
    </Shell>
  );
}

function stateClass(state: string): string {
  if (state.includes("FAILED")) return "HIGH";
  if (state === "DEPLOYED") return "LOW";
  if (state === "ROLLED_BACK") return "MEDIUM";
  return "";
}

function diffLineClass(line: string): string {
  if (line.startsWith("+") && !line.startsWith("+++")) return "add";
  if (line.startsWith("-") && !line.startsWith("---")) return "del";
  if (line.startsWith("@@")) return "hunk";
  return "";
}
