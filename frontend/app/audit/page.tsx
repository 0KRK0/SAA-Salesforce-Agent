"use client";

import { useEffect, useState } from "react";
import { Shell } from "@/components/Shell";
import { api } from "@/lib/api";
import type { AuditEntry } from "@/lib/types";

export default function AuditPage() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [deployments, setDeployments] = useState<Array<Record<string, unknown>>>([]);

  useEffect(() => {
    (async () => {
      try {
        const [audit, deps] = await Promise.all([api.audit(), api.deployments()]);
        setEntries(audit.entries);
        setDeployments(deps.deployments);
      } catch {
        /* login gate handles unauthenticated */
      }
    })();
  }, []);

  return (
    <Shell active="audit" topbar={<strong>Audit history</strong>}>
      <div className="content">
        <div className="page" style={{ maxWidth: 1100 }}>
          <div className="card">
            <h3>Deployments</h3>
            {!deployments.length && <p className="muted">No deployments yet.</p>}
            {!!deployments.length && (
              <table className="grid">
                <thead>
                  <tr>
                    <th>Deploy id</th>
                    <th>Mode</th>
                    <th>Status</th>
                    <th>Components</th>
                    <th>Tests</th>
                    <th>Verified</th>
                    <th>When</th>
                  </tr>
                </thead>
                <tbody>
                  {deployments.map((d) => (
                    <tr key={String(d.id)}>
                      <td>
                        <code>{String(d.salesforce_deploy_id ?? "—")}</code>
                      </td>
                      <td>{d.check_only ? "validate" : "deploy"}</td>
                      <td>{String(d.status)}</td>
                      <td>
                        {String(d.components_total)} / {String(d.components_failed)} failed
                      </td>
                      <td>
                        {String(d.tests_total)} / {String(d.tests_failed)} failed
                      </td>
                      <td>{d.verified ? "yes" : "no"}</td>
                      <td className="muted">
                        {new Date(String(d.created_at)).toLocaleString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="card">
            <h3>Audit log</h3>
            {!entries.length && <p className="muted">Nothing recorded yet.</p>}
            {!!entries.length && (
              <table className="grid">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Action</th>
                    <th>Object</th>
                    <th>Risk</th>
                    <th>Approval</th>
                    <th>Execution</th>
                    <th>Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((e) => (
                    <tr key={e.id}>
                      <td className="muted">{new Date(e.created_at).toLocaleString()}</td>
                      <td>
                        {e.action}
                        {e.record_ids?.length ? (
                          <div className="muted" style={{ fontSize: 12 }}>
                            <code>{e.record_ids.join(", ")}</code>
                          </div>
                        ) : null}
                      </td>
                      <td>{e.salesforce_object ?? "—"}</td>
                      <td>
                        {e.risk_level ? (
                          <span className={`badge ${e.risk_level}`}>{e.risk_level}</span>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="muted">{e.approval_state ?? "—"}</td>
                      <td className="muted">{e.execution_state ?? "—"}</td>
                      <td style={{ color: e.outcome === "ok" ? "var(--ok)" : "var(--danger)" }}>
                        {e.outcome}
                        {e.error ? (
                          <div className="muted" style={{ fontSize: 12 }}>
                            {e.error}
                          </div>
                        ) : null}
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
