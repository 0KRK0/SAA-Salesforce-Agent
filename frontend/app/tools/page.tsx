"use client";

import { useEffect, useMemo, useState } from "react";
import { Shell } from "@/components/Shell";
import { api } from "@/lib/api";
import type { ToolCatalogEntry } from "@/lib/types";

/**
 * What the agent can actually do, and under what controls.
 *
 * This page exists because "can it delete things?" is the first question every
 * security reviewer asks. The answer should be a page they can read, not a
 * promise — so each tool shows its risk, whether it requires approval, whether
 * it mutates, and which provider supplied it.
 */
export default function ToolsPage() {
  const [tools, setTools] = useState<ToolCatalogEntry[]>([]);
  const [providers, setProviders] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .tools()
      .then((r) => {
        setTools(r.tools);
        setProviders(r.providers);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load"));
  }, []);

  const groups = useMemo(() => {
    const term = filter.trim().toLowerCase();
    const matching = tools.filter(
      (t) =>
        !term ||
        t.name.includes(term) ||
        t.description.toLowerCase().includes(term) ||
        t.tags.some((tag) => tag.includes(term)),
    );
    const byTag = new Map<string, ToolCatalogEntry[]>();
    for (const tool of matching) {
      const key = tool.provider !== "native" ? "external (MCP)" : primaryTag(tool.tags);
      byTag.set(key, [...(byTag.get(key) ?? []), tool]);
    }
    return [...byTag.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [tools, filter]);

  const mutating = tools.filter((t) => t.mutating);
  const gated = mutating.filter((t) => t.requires_approval);

  return (
    <Shell active="tools" topbar={<strong>Capabilities</strong>}>
      <div className="content">
        <div className="page stack">
          {error && <div className="banner error">{error}</div>}

          <div className="card">
            <h3 style={{ marginTop: 0 }}>What this agent can do</h3>
            <p className="muted" style={{ fontSize: 13 }}>
              {tools.length} tools are registered, of which {mutating.length} can change
              the org. {gated.length} of those require explicit human approval before
              they execute — the remainder are gated by the risk engine at runtime
              depending on the target org and the arguments.
            </p>
            <p className="muted" style={{ fontSize: 13 }}>
              Providers: {providers.join(", ")}. Tools from an external MCP server pass
              through exactly the same risk classification, approval gate, audit trail
              and verification as first-party ones, and their results are treated as
              untrusted data.
            </p>
            <input
              placeholder="Filter by name, description or tag…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </div>

          {groups.map(([group, entries]) => (
            <div className="card" key={group}>
              <h3 style={{ marginTop: 0, textTransform: "capitalize" }}>{group}</h3>
              <table className="table">
                <thead>
                  <tr>
                    <th>Tool</th>
                    <th>Risk</th>
                    <th>Approval</th>
                    <th>Effect</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((t) => (
                    <tr key={t.name} style={{ opacity: t.enabled ? 1 : 0.45 }}>
                      <td>
                        <code>{t.name}</code>
                        <div className="muted" style={{ fontSize: 12 }}>
                          {t.description}
                        </div>
                        {!t.enabled && (
                          <span className="badge">disabled for this project</span>
                        )}
                      </td>
                      <td>
                        <span className={`badge ${t.risk}`}>{t.risk}</span>
                      </td>
                      <td>{t.requires_approval ? "Required" : "Not required"}</td>
                      <td>
                        {t.mutating ? "Changes the org" : "Read-only"}
                        {t.long_running && (
                          <div className="muted" style={{ fontSize: 12 }}>
                            long-running
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      </div>
    </Shell>
  );
}

function primaryTag(tags: string[]): string {
  const order = [
    "security",
    "deployment",
    "apex",
    "flow",
    "data",
    "reports",
    "metadata",
    "diagnostics",
    "knowledge",
  ];
  for (const tag of order) if (tags.includes(tag)) return tag;
  return tags[0] ?? "other";
}
