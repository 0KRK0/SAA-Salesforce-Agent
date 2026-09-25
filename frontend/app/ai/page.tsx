"use client";

import { useEffect, useState } from "react";
import { Shell } from "@/components/Shell";
import { api } from "@/lib/api";
import type {
  LLMCredential,
  ModelTier,
  ProviderSpec,
  RoutingInfo,
  UsageReport,
} from "@/lib/types";

const TIERS: ModelTier[] = ["FAST", "BALANCED", "ADVANCED"];

/**
 * AI providers.
 *
 * Three honesty rules shape this page:
 *
 *  1. A key is write-only. It is posted once and never rendered again — there
 *     is no reveal control here because there is no endpoint behind one.
 *  2. "Connected" is only ever shown after a real call succeeded. Until then
 *     the credential reads "Not tested", not "Connected".
 *  3. Providers that are declared but not built are listed as unavailable with
 *     the reason, rather than hidden or shown as clickable.
 */
export default function AiPage() {
  const [providers, setProviders] = useState<ProviderSpec[]>([]);
  const [credentials, setCredentials] = useState<LLMCredential[]>([]);
  const [routing, setRouting] = useState<RoutingInfo | null>(null);
  const [usage, setUsage] = useState<UsageReport | null>(null);
  const [byokRequired, setByokRequired] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);

  async function load() {
    try {
      const [p, c, r, u] = await Promise.all([
        api.aiProviders(),
        api.aiCredentials(),
        api.aiRouting(),
        api.aiUsage(),
      ]);
      setProviders(p.providers);
      setByokRequired(p.byok_required);
      setCredentials(c.credentials);
      setRouting(r);
      setUsage(u);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load AI settings");
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function test(id: string) {
    setTesting(id);
    setError(null);
    try {
      const result = await api.testAiCredential(id);
      if (!result.success) setError(result.message ?? "The connection test failed.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The connection test failed.");
    } finally {
      setTesting(null);
    }
  }

  async function remove(id: string) {
    setBusy(true);
    try {
      await api.deleteAiCredential(id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setBusy(false);
    }
  }

  const available = providers.filter((p) => p.implemented && p.permitted_by_policy);

  return (
    <Shell active="ai" topbar={<strong>AI providers</strong>}>
      <div className="content">
        <div className="page stack">
          {error && <div className="banner error">{error}</div>}

          {byokRequired && !credentials.length && (
            <div className="banner">
              This deployment does not provide a shared model key. Add your own
              provider key below — your requests then run on your account, under
              your own terms with that vendor.
            </div>
          )}

          <RoutingCard routing={routing} />

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Your provider keys</h3>
            <p className="muted" style={{ fontSize: 13 }}>
              Keys are stored as references in this deployment&rsquo;s secret store,
              bound to this project. They are never returned to the browser after
              you submit them, and nothing here can reveal one.
            </p>

            {credentials.length ? (
              <table className="table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Provider</th>
                    <th>Key</th>
                    <th>Models</th>
                    <th>Connection</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {credentials.map((c) => (
                    <tr key={c.id}>
                      <td>
                        {c.name}
                        {c.is_default && (
                          <span className="badge" style={{ marginLeft: 6 }}>
                            default
                          </span>
                        )}
                      </td>
                      <td>{c.provider}</td>
                      <td className="key-hint">
                        {c.has_key ? `stored · ${c.key_fingerprint}` : "none"}
                      </td>
                      <td className="key-hint">
                        {Object.entries(c.tier_models ?? {})
                          .map(([tier, model]) => `${tier}: ${model}`)
                          .join(" · ") || "provider defaults"}
                      </td>
                      <td>
                        <ConnectionState credential={c} />
                      </td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <button
                          className="ghost"
                          disabled={testing === c.id}
                          onClick={() => void test(c.id)}
                        >
                          {testing === c.id ? "Testing…" : "Test"}
                        </button>
                        <button
                          className="ghost"
                          disabled={busy}
                          onClick={() => void remove(c.id)}
                          style={{ marginLeft: 6 }}
                        >
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="empty" style={{ padding: "18px 0" }}>
                No provider keys yet.
              </div>
            )}

            <AddCredential
              providers={available}
              onAdded={() => void load()}
              onError={setError}
            />
          </div>

          <ProviderCatalog providers={providers} />
          <UsageCard usage={usage} />
        </div>
      </div>
    </Shell>
  );
}

/** Never claims "Connected" on the strength of a stored key alone. */
function ConnectionState({ credential }: { credential: LLMCredential }) {
  if (credential.last_test_ok === true) {
    return <span className="badge ok">Connected</span>;
  }
  if (credential.last_test_ok === false) {
    return (
      <span
        className="badge HIGH"
        title={credential.last_test_error ?? "The last test failed."}
      >
        Failed
      </span>
    );
  }
  return <span className="badge unknown">Not tested</span>;
}

function RoutingCard({ routing }: { routing: RoutingInfo | null }) {
  if (!routing) return null;
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Where this project&rsquo;s requests go</h3>
      {routing.available && routing.primary ? (
        <>
          <table className="table">
            <tbody>
              <tr>
                <td>Model</td>
                <td>
                  {routing.primary.provider} · {routing.primary.model}
                </td>
              </tr>
              <tr>
                <td>Key</td>
                <td>
                  {routing.primary.byok
                    ? "Your own provider key"
                    : "This deployment's shared key"}
                </td>
              </tr>
              <tr>
                <td>Fallback</td>
                <td>
                  {routing.fallback_enabled
                    ? routing.fallbacks.map((f) => f.provider).join(", ") || "none configured"
                    : "Off"}
                  <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                    {routing.fallback_note}
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </>
      ) : (
        <div className="banner">{routing.message}</div>
      )}
    </div>
  );
}

function ProviderCatalog({ providers }: { providers: ProviderSpec[] }) {
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Supported providers</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Providers that are not built are listed here with the reason, rather than
        left out. Any model name the vendor offers can be used — the names shown
        are only what a tier falls back to.
      </p>
      <div className="provider-grid">
        {providers.map((p) => (
          <div
            key={p.kind}
            className={`provider ${p.implemented && p.permitted_by_policy ? "" : "unavailable"}`}
          >
            <h4>
              {p.label}{" "}
              {!p.implemented && <span className="badge unknown">not built</span>}
              {p.implemented && !p.permitted_by_policy && (
                <span className="badge unknown">blocked by policy</span>
              )}
            </h4>
            <p>{p.notes || defaultsLine(p)}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function defaultsLine(spec: ProviderSpec): string {
  const entries = Object.entries(spec.default_models ?? {});
  if (!entries.length) return "Any model this provider offers.";
  return entries.map(([tier, model]) => `${tier}: ${model}`).join(" · ");
}

function UsageCard({ usage }: { usage: UsageReport | null }) {
  if (!usage) return null;
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Usage — last {usage.days} days</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        {usage.pricing.note}
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>Provider</th>
            <th>Model</th>
            <th>Requests</th>
            <th>Input</th>
            <th>Output</th>
            <th>Estimated cost</th>
          </tr>
        </thead>
        <tbody>
          {usage.breakdown.map((row) => (
            <tr key={`${row.provider}-${row.model}-${String(row.byok)}`}>
              <td>{row.provider}</td>
              <td className="key-hint">{row.model}</td>
              <td>{row.requests.toLocaleString()}</td>
              <td>{row.input_tokens.toLocaleString()}</td>
              <td>{row.output_tokens.toLocaleString()}</td>
              <td>
                {row.estimated_cost_usd > 0
                  ? `$${row.estimated_cost_usd.toFixed(4)}`
                  : "—"}
              </td>
            </tr>
          ))}
          {!usage.breakdown.length && (
            <tr>
              <td colSpan={6} className="muted">
                No model calls recorded yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function AddCredential({
  providers,
  onAdded,
  onError,
}: {
  providers: ProviderSpec[];
  onAdded: () => void;
  onError: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState("");
  const [name, setName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [models, setModels] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const spec = providers.find((p) => p.kind === kind);

  if (!open) {
    return (
      <button
        className="primary"
        style={{ marginTop: 12 }}
        disabled={!providers.length}
        onClick={() => setOpen(true)}
      >
        Add a provider key
      </button>
    );
  }

  return (
    <form
      className="stack"
      style={{ marginTop: 14 }}
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try {
          await api.createAiCredential({
            name,
            provider: kind,
            api_key: apiKey || undefined,
            base_url: baseUrl || undefined,
            tier_models: Object.keys(models).length ? models : undefined,
            is_default: true,
          });
          // The key exists only in this component's state; drop it immediately
          // rather than leaving it in memory behind a closed form.
          setApiKey("");
          setOpen(false);
          onAdded();
        } catch (err) {
          onError(err instanceof Error ? err.message : "Could not save the key");
        } finally {
          setBusy(false);
        }
      }}
    >
      <select value={kind} onChange={(e) => setKind(e.target.value)} required>
        <option value="">Choose a provider…</option>
        {providers.map((p) => (
          <option key={p.kind} value={p.kind}>
            {p.label}
          </option>
        ))}
      </select>

      <input
        placeholder="A name for this key, e.g. 'Production Anthropic'"
        value={name}
        onChange={(e) => setName(e.target.value)}
        required
      />

      {spec?.requires_api_key !== false && (
        <input
          type="password"
          autoComplete="off"
          placeholder="API key"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          required
        />
      )}

      {spec?.requires_base_url && (
        <input
          placeholder={
            spec.kind === "AZURE_OPENAI"
              ? "https://<resource>.openai.azure.com"
              : "Endpoint URL"
          }
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          required
        />
      )}

      {spec && (
        <div className="stack">
          {TIERS.map((tier) => (
            <input
              key={tier}
              placeholder={`${tier} model${
                spec.default_models?.[tier] ? ` (default ${spec.default_models[tier]})` : ""
              }`}
              value={models[tier] ?? ""}
              onChange={(e) =>
                setModels((m) => ({ ...m, [tier]: e.target.value }))
              }
            />
          ))}
        </div>
      )}

      {spec?.notes && (
        <div className="muted" style={{ fontSize: 12 }}>
          {spec.notes}
        </div>
      )}

      <div className="row-between">
        <button className="primary" disabled={busy || !kind || !name}>
          {busy ? "Saving…" : "Save key"}
        </button>
        <button type="button" className="ghost" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}
