"use client";

import { useCallback, useEffect, useState } from "react";
import { Shell } from "@/components/Shell";
import { api } from "@/lib/api";
import type { Connection } from "@/lib/types";

export default function ConnectionsPage() {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [config, setConfig] = useState<{ configured: boolean; setup_hint: string | null } | null>(
    null,
  );
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [conns, cfg] = await Promise.all([api.connections(), api.salesforceConfig()]);
      setConnections(conns);
      setConfig(cfg);
    } catch {
      /* login gate handles unauthenticated */
    }
  }, []);

  useEffect(() => {
    void load();
    const params = new URLSearchParams(window.location.search);
    if (params.get("connected")) setMessage("Salesforce org connected.");
    if (params.get("error")) setError(`Connection failed: ${params.get("error")}`);
  }, [load]);

  async function connect(sandbox: boolean) {
    setError(null);
    try {
      const { authorize_url } = await api.startOAuth(sandbox);
      window.location.href = authorize_url;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the OAuth flow");
    }
  }

  return (
    <Shell active="connections" topbar={<strong>Salesforce connections</strong>}>
      <div className="content">
        <div className="page">
          {message && <div className="banner">{message}</div>}
          {error && <div className="banner error">{error}</div>}
          {config && !config.configured && (
            <div className="banner">{config.setup_hint}</div>
          )}

          <div className="card">
            <h3>Connect an org</h3>
            <p className="muted" style={{ fontSize: 13 }}>
              OAuth 2.0 with PKCE. Tokens are encrypted at rest on the server and are never
              sent to the model. Sandbox-first is strongly recommended: metadata changes to
              production orgs are blocked unless the deployment policy allows them.
            </p>
            <div style={{ display: "flex", gap: 8 }}>
              <button className="primary" disabled={!config?.configured} onClick={() => connect(true)}>
                Connect sandbox
              </button>
              <button disabled={!config?.configured} onClick={() => connect(false)}>
                Connect production
              </button>
            </div>
          </div>

          <div className="card">
            <h3>Connected orgs</h3>
            {!connections.length && <p className="muted">No orgs connected yet.</p>}
            {!!connections.length && (
              <table className="grid">
                <thead>
                  <tr>
                    <th>Org</th>
                    <th>User</th>
                    <th>Type</th>
                    <th>Validated</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {connections.map((c) => (
                    <tr key={c.id}>
                      <td>
                        <strong>{c.label || c.sf_org_id}</strong>
                        <div className="muted" style={{ fontSize: 12 }}>
                          <code>{c.sf_org_id}</code> · {c.instance_url}
                        </div>
                        {c.token_fingerprint && (
                          <div className="key-hint" style={{ marginTop: 2 }}>
                            token {c.token_fingerprint}
                            {c.has_refresh_token ? " · refreshable" : " · no refresh token"}
                          </div>
                        )}
                      </td>
                      <td>{c.username}</td>
                      <td>
                        {/* The declared environment, not the sandbox flag. A
                            sandbox can legitimately be a team's UAT, and the
                            environment is what policy keys off. */}
                        <span className={`badge ${c.environment}`}>{c.environment}</span>
                        {c.environment !== "PRODUCTION" && !c.is_sandbox && (
                          <div
                            className="muted"
                            style={{ fontSize: 11, marginTop: 4 }}
                            title="Production controls apply regardless of the label."
                          >
                            Salesforce reports this org is not a sandbox
                          </div>
                        )}
                      </td>
                      <td className="muted">
                        {c.last_validated_at
                          ? new Date(c.last_validated_at).toLocaleString()
                          : "never"}
                      </td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <button
                          className="ghost"
                          onClick={async () => {
                            try {
                              await api.validateConnection(c.id);
                              setMessage("Connection is healthy.");
                              await load();
                            } catch (err) {
                              setError(err instanceof Error ? err.message : "Validation failed");
                            }
                          }}
                        >
                          Validate
                        </button>{" "}
                        <button
                          className="danger"
                          onClick={async () => {
                            await api.disconnect(c.id);
                            await load();
                          }}
                        >
                          Disconnect
                        </button>
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
