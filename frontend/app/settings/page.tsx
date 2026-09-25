"use client";

import { useEffect, useState } from "react";
import { Shell } from "@/components/Shell";
import { ApiError, api } from "@/lib/api";
import type { KnowledgeEntry, ProductionPosture, SalesforceApp } from "@/lib/types";

/**
 * Organization settings: the agent policy, the members who can approve, the
 * external tool providers, and what the agent has learned about the org.
 *
 * The policy section deliberately shows *stored* and *effective* side by side.
 * A tenant setting can only ever be as permissive as the deployment allows, and
 * an admin who cannot see that difference will assume a setting took effect
 * when it did not.
 */
export default function SettingsPage() {
  const [policy, setPolicy] = useState<Record<string, unknown> | null>(null);
  const [members, setMembers] = useState<Array<Record<string, unknown>>>([]);
  const [servers, setServers] = useState<Array<Record<string, unknown>>>([]);
  const [mcpEnabled, setMcpEnabled] = useState(true);
  const [knowledge, setKnowledge] = useState<KnowledgeEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function load() {
    try {
      const [p, m, s, k] = await Promise.all([
        api.policy(),
        api.members(),
        api.mcpServers(),
        api.knowledge(),
      ]);
      setPolicy(p as unknown as Record<string, unknown>);
      setMembers(m.members);
      setServers(s.servers);
      setMcpEnabled(s.mcp_enabled);
      setKnowledge(k.entries);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load settings");
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const stored = (policy?.stored ?? {}) as Record<string, unknown>;
  const effective = (policy?.effective ?? {}) as Record<string, unknown>;
  const ceilings = (policy?.deployment_ceilings ?? {}) as Record<string, unknown>;
  const production = policy?.production as ProductionPosture | undefined;
  const companyLevel = policy?.company as
    | { name: string; allow_production_mutations: boolean; you_can_change_it: boolean }
    | undefined;

  async function patch(field: string, value: unknown) {
    setSaving(true);
    setError(null);
    try {
      await api.updatePolicy({ [field]: value });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Update failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Shell active="settings" topbar={<strong>Organization</strong>}>
      <div className="content">
        <div className="page stack">
          {error && <div className="banner error">{error}</div>}

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Agent policy</h3>
            <p className="muted" style={{ fontSize: 13 }}>
              These controls sit outside the model. Claude can propose anything; what
              is permitted here is decided by this policy and enforced by the risk
              engine before any tool runs.
            </p>
            {production && !production.permitted && (
              <ProductionBlockedNotice posture={production} onChanged={load} />
            )}
            <table className="table">
              <thead>
                <tr>
                  <th>Setting</th>
                  <th>This project</th>
                  <th>In effect</th>
                  <th>Deployment ceiling</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>
                    Allow production mutations
                    <div className="muted" style={{ fontSize: 12 }}>
                      Metadata changes against a non-sandbox org. Three levels must
                      all permit it — this project, {companyLevel?.name ?? "your company"},
                      and the deployment.
                    </div>
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      disabled={saving}
                      checked={Boolean(stored.allow_production_mutations)}
                      onChange={(e) =>
                        void patch("allow_production_mutations", e.target.checked)
                      }
                    />
                  </td>
                  <td>
                    {effective.allow_production_mutations ? (
                      "yes"
                    ) : (
                      <>
                        no
                        {production?.blocked_by && (
                          <div className="muted" style={{ fontSize: 12 }}>
                            blocked at the {production.blocked_by} level
                          </div>
                        )}
                      </>
                    )}
                  </td>
                  <td>{ceilings.allow_production_mutations ? "permitted" : "blocked"}</td>
                </tr>
                <tr>
                  <td>
                    Separate approver required
                    <div className="muted" style={{ fontSize: 12 }}>
                      The requester cannot approve their own high-risk change.
                    </div>
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      disabled={saving}
                      checked={Boolean(stored.require_separate_approver)}
                      onChange={(e) =>
                        void patch("require_separate_approver", e.target.checked)
                      }
                    />
                  </td>
                  <td>{effective.require_separate_approver ? "yes" : "no"}</td>
                  <td>—</td>
                </tr>
                <tr>
                  <td>
                    Approval lifetime (seconds)
                    <div className="muted" style={{ fontSize: 12 }}>
                      After this, an approval no longer authorizes anything.
                    </div>
                  </td>
                  <td>
                    <NumberField
                      value={Number(stored.approval_ttl_seconds ?? 3600)}
                      disabled={saving}
                      onCommit={(v) => void patch("approval_ttl_seconds", v)}
                    />
                  </td>
                  <td>{String(effective.approval_ttl_seconds ?? "")}</td>
                  <td>{String(ceilings.approval_ttl_seconds ?? "")}</td>
                </tr>
                <tr>
                  <td>
                    Maximum agent steps
                    <div className="muted" style={{ fontSize: 12 }}>
                      Runaway-loop protection.
                    </div>
                  </td>
                  <td>
                    <NumberField
                      value={Number(stored.max_agent_steps ?? 20)}
                      disabled={saving}
                      onCommit={(v) => void patch("max_agent_steps", v)}
                    />
                  </td>
                  <td>{String(effective.max_agent_steps ?? "")}</td>
                  <td>{String(ceilings.max_agent_steps ?? "")}</td>
                </tr>
                <tr>
                  <td>
                    Maximum records per bulk change
                    <div className="muted" style={{ fontSize: 12 }}>
                      A data mutation above this is refused outright.
                    </div>
                  </td>
                  <td>
                    <NumberField
                      value={Number(stored.max_bulk_records ?? 50000)}
                      disabled={saving}
                      onCommit={(v) => void patch("max_bulk_records", v)}
                    />
                  </td>
                  <td>{String(effective.max_bulk_records ?? "")}</td>
                  <td>{String(ceilings.max_bulk_records ?? "")}</td>
                </tr>
              </tbody>
            </table>
            {!!ceilings.note && (
              <p className="muted" style={{ fontSize: 12 }}>
                {String(ceilings.note)}
              </p>
            )}
          </div>

          <SalesforceAppCard />

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Members</h3>
            <p className="muted" style={{ fontSize: 13 }}>
              Roles decide who can approve what. Security changes and production
              deployments require specific roles, and some require two people.
            </p>
            <table className="table">
              <thead>
                <tr>
                  <th>Email</th>
                  <th>Role</th>
                  <th>Identity</th>
                </tr>
              </thead>
              <tbody>
                {members.map((m) => (
                  <tr key={String(m.user_id)}>
                    <td>{String(m.email)}</td>
                    <td>
                      <span className="badge">{String(m.role)}</span>
                    </td>
                    <td className="muted">{String(m.idp)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>External tool providers (MCP)</h3>
            {!mcpEnabled && (
              <div className="banner">
                MCP support is disabled for this deployment.
              </div>
            )}
            {!servers.length && (
              <p className="muted" style={{ fontSize: 13 }}>
                No MCP servers are registered. Registering one adds its tools to the
                agent — subject to the same risk engine, approval gate and audit trail
                as first-party tools, with its results treated as untrusted data.
              </p>
            )}
            {servers.map((s) => (
              <div key={String(s.id)} className="row-between" style={{ padding: "6px 0" }}>
                <div>
                  <strong>{String(s.name)}</strong>{" "}
                  <span className="muted" style={{ fontSize: 12 }}>
                    {String(s.transport)} ·{" "}
                    {Array.isArray(s.discovered_tools) ? s.discovered_tools.length : 0}{" "}
                    tools
                  </span>
                  {!!s.last_error && (
                    <div className="banner error" style={{ marginTop: 4 }}>
                      {String(s.last_error)}
                    </div>
                  )}
                </div>
                <button
                  className="ghost"
                  onClick={async () => {
                    try {
                      await api.discoverMcpServer(String(s.id));
                      await load();
                    } catch (err) {
                      setError(err instanceof Error ? err.message : "Discovery failed");
                    }
                  }}
                >
                  Re-discover
                </button>
              </div>
            ))}
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>What the agent knows about your orgs</h3>
            <p className="muted" style={{ fontSize: 13 }}>
              Observations recorded from the org itself — schema, deployment outcomes,
              past failures — recalled when relevant to a request. The agent&apos;s own
              conclusions are never stored here.
            </p>
            {!knowledge.length && (
              <p className="muted" style={{ fontSize: 13 }}>
                Nothing recorded yet.
              </p>
            )}
            {!!knowledge.length && (
              <table className="table">
                <thead>
                  <tr>
                    <th>Kind</th>
                    <th>Subject</th>
                    <th>Observation</th>
                    <th>Source</th>
                  </tr>
                </thead>
                <tbody>
                  {knowledge.slice(0, 50).map((k) => (
                    <tr key={k.id}>
                      <td>
                        <span className="badge">{k.kind}</span>
                      </td>
                      <td>
                        <code>{k.key}</code>
                      </td>
                      <td>{k.summary}</td>
                      <td className="muted">{k.source}</td>
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

/**
 * Why production is blocked, and the one thing that would unblock it.
 *
 * The screen this replaces showed a ticked project checkbox next to an
 * effective value of "no" and left the reader to work out why. The model,
 * reading the same ambiguity, told a user to ask a project administrator to
 * adjust the policy — advice that could not have worked, because the
 * deployment ceiling was refusing. So this names the level, names who can
 * change it, and where that level is the company's own clearance, offers the
 * control inline instead of describing it.
 */
function ProductionBlockedNotice({
  posture,
  onChanged,
}: {
  posture: ProductionPosture;
  onChanged: () => Promise<void> | void;
}) {
  const level = posture.levels.find((l) => l.level === posture.blocked_by);
  const [expanded, setExpanded] = useState(false);
  const [confirm, setConfirm] = useState("");
  const [companyName, setCompanyName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (posture.blocked_by !== "company") return;
    void api
      .productionAccess()
      .then((d) => setCompanyName(d.confirmation_phrase))
      .catch(() => setCompanyName(""));
  }, [posture.blocked_by]);

  if (!level) return null;

  async function grant() {
    setBusy(true);
    setError(null);
    try {
      await api.setProductionAccess(true, confirm);
      setExpanded(false);
      setConfirm("");
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not enable it");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="banner" style={{ marginBottom: 14 }}>
      <strong>Production changes are blocked at the {level.level} level.</strong>
      <div style={{ marginTop: 6, fontSize: 13 }}>
        Changed by {level.changed_by}. {level.how}
      </div>

      {posture.blocked_by === "company" && companyName && (
        <div style={{ marginTop: 12 }}>
          {expanded ? (
            <div className="stack" style={{ gap: 8 }}>
              <label style={{ fontSize: 13 }}>
                Type <strong>{companyName}</strong> to clear this company for
                production changes.
              </label>
              <input
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                placeholder={companyName}
                style={{ maxWidth: 420 }}
              />
              {error && <div className="banner error">{error}</div>}
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  type="button"
                  disabled={busy || confirm !== companyName}
                  onClick={() => void grant()}
                >
                  Clear for production
                </button>
                <button
                  type="button"
                  className="ghost"
                  onClick={() => {
                    setExpanded(false);
                    setConfirm("");
                  }}
                >
                  Cancel
                </button>
              </div>
              <p className="muted" style={{ fontSize: 12, margin: 0 }}>
                {posture.note}
              </p>
            </div>
          ) : (
            <button type="button" className="ghost" onClick={() => setExpanded(true)}>
              Clear this company for production…
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Your company's own Salesforce External Client App.
 *
 * The reason this screen exists rather than an environment variable: a
 * Connected App is the Salesforce administrator's control over who reaches
 * their org — which profiles, which IP ranges, how long a refresh token lives,
 * and the one button that revokes every session. A single shared app in the
 * vendor's environment makes that the vendor's control, and couples every
 * customer to one revocation.
 *
 * The consumer secret is write-only, exactly as it is on the server. It is
 * submitted once, stored by the secret store, and no endpoint returns it — so
 * this form shows a fingerprint instead, which is enough to confirm a rotation
 * happened and not enough to use.
 */
function SalesforceAppCard() {
  const [apps, setApps] = useState<SalesforceApp[]>([]);
  const [meta, setMeta] = useState<{
    enabled: boolean;
    required: boolean;
    deployment_app_available: boolean;
    callback_url: string;
    required_scopes: string[];
    instructions: string;
  } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState("Our Salesforce app");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [loginUrl, setLoginUrl] = useState("https://login.salesforce.com");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function load() {
    try {
      const data = await api.salesforceApps();
      setApps(data.apps);
      setMeta({
        enabled: data.enabled,
        required: data.required,
        deployment_app_available: data.deployment_app_available,
        callback_url: data.callback_url,
        required_scopes: data.required_scopes,
        instructions: data.instructions,
      });
    } catch (err) {
      // A non-admin simply does not get this section, which is not an error
      // worth showing them.
      if (err instanceof ApiError && err.status === 403) return;
      setError(err instanceof Error ? err.message : "Could not load Salesforce apps");
    }
  }

  useEffect(() => {
    void load();
  }, []);

  function reset() {
    setEditing(null);
    setName("Our Salesforce app");
    setClientId("");
    setClientSecret("");
    setLoginUrl("https://login.salesforce.com");
  }

  function edit(app: SalesforceApp) {
    setEditing(app.id);
    setName(app.name);
    setClientId(app.client_id);
    // Never prefilled: the server does not return it, and a masked value in
    // the field would be submitted back as if it were real.
    setClientSecret("");
    setLoginUrl(app.login_url);
  }

  async function save() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const body = {
        name,
        client_id: clientId.trim(),
        login_url: loginUrl.trim(),
        is_default: true,
        ...(clientSecret ? { client_secret: clientSecret } : {}),
      };
      if (editing) {
        await api.updateSalesforceApp(editing, body);
        setNotice(
          clientSecret
            ? "Updated, and the consumer secret was replaced. Existing connections keep working."
            : "Updated. The stored consumer secret was left unchanged.",
        );
      } else {
        await api.createSalesforceApp(body);
        setNotice("Registered. New org connections will use your app.");
      }
      reset();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  async function remove(app: SalesforceApp) {
    setBusy(true);
    setError(null);
    try {
      const result = await api.deleteSalesforceApp(app.id);
      setNotice(result.message);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove");
    } finally {
      setBusy(false);
    }
  }

  // A non-admin gets no section at all, which is correct — but a *failed* load
  // must not vanish the same way, or the only symptom of a broken endpoint is
  // a section that silently is not there.
  if (!meta) {
    return error ? (
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Salesforce app</h3>
        <div className="banner error">{error}</div>
      </div>
    ) : null;
  }

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Salesforce app</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        The External Client App your orgs are reached through. Registering your own
        keeps that control with your Salesforce administrator: permitted profiles, IP
        ranges, refresh-token lifetime, and the ability to revoke every session here
        in one click.
        {meta.deployment_app_available && !meta.required
          ? " Without one, connections use this deployment's shared app."
          : ""}
        {meta.required
          ? " This deployment requires each company to bring its own."
          : ""}
      </p>

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner">{notice}</div>}

      <div className="stack" style={{ gap: 8, marginBottom: 16 }}>
        <label className="muted" style={{ fontSize: 12 }}>
          Callback URL to register in Salesforce
        </label>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <code
            style={{
              fontSize: 12,
              padding: "6px 10px",
              borderRadius: 6,
              background: "rgba(127,127,127,.12)",
              wordBreak: "break-all",
            }}
          >
            {meta.callback_url}
          </code>
          <button
            type="button"
            className="ghost"
            onClick={() => {
              void navigator.clipboard?.writeText(meta.callback_url);
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1500);
            }}
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          Paste this exactly. It is generated from the route this server actually
          serves, so it cannot drift out of step with it. Scopes to select:{" "}
          <strong>{meta.required_scopes.join(", ")}</strong>.
        </p>
      </div>

      {apps.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Consumer key</th>
              <th>Secret</th>
              <th>Scope</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {apps.map((a) => (
              <tr key={a.id}>
                <td>{a.name}</td>
                <td className="muted" style={{ fontSize: 12, wordBreak: "break-all" }}>
                  {a.client_id}
                </td>
                <td className="muted" style={{ fontSize: 12 }}>
                  {a.has_client_secret ? `set · ${a.client_secret_fingerprint}` : "none"}
                </td>
                <td>
                  <span className="badge">{a.scope}</span>
                </td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <button type="button" className="ghost" onClick={() => edit(a)}>
                    Edit
                  </button>{" "}
                  <button
                    type="button"
                    className="ghost"
                    disabled={busy}
                    onClick={() => void remove(a)}
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {meta.enabled ? (
        <div className="stack" style={{ gap: 10, marginTop: 16 }}>
          <strong style={{ fontSize: 14 }}>
            {editing ? "Edit app" : "Register your app"}
          </strong>
          <input
            placeholder="Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <input
            placeholder="Consumer Key"
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
          />
          <input
            type="password"
            autoComplete="new-password"
            placeholder={
              editing ? "Consumer Secret (leave blank to keep the stored one)" : "Consumer Secret"
            }
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
          />
          <input
            placeholder="Login URL"
            value={loginUrl}
            onChange={(e) => setLoginUrl(e.target.value)}
          />
          <p className="muted" style={{ fontSize: 12, margin: 0 }}>
            Use <code>https://test.salesforce.com</code> for a sandbox, or your My
            Domain URL. The secret is sent once and held by the secret store; nothing
            reads it back, including this page.
          </p>
          <div style={{ display: "flex", gap: 8 }}>
            <button
              type="button"
              disabled={busy || clientId.trim().length < 10}
              onClick={() => void save()}
            >
              {editing ? "Save changes" : "Register app"}
            </button>
            {editing && (
              <button type="button" className="ghost" onClick={reset}>
                Cancel
              </button>
            )}
          </div>
        </div>
      ) : (
        <p className="muted" style={{ fontSize: 13 }}>
          Customer-owned Salesforce apps are switched off in this deployment, so
          connections use the shared app.
        </p>
      )}
    </div>
  );
}

function NumberField({
  value,
  disabled,
  onCommit,
}: {
  value: number;
  disabled: boolean;
  onCommit: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  return (
    <input
      type="number"
      disabled={disabled}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        const parsed = Number(draft);
        if (Number.isFinite(parsed) && parsed !== value) onCommit(parsed);
      }}
      style={{ width: 120 }}
    />
  );
}
