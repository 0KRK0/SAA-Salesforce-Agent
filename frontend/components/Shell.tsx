"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { Conversation, ProjectSummary, UserOut } from "@/lib/types";

interface ShellProps {
  children: React.ReactNode;
  active:
    | "chat"
    | "connections"
    | "audit"
    | "releases"
    | "tools"
    | "ai"
    | "settings";
  conversations?: Conversation[];
  currentConversationId?: string | null;
  onSelectConversation?: (id: string) => void;
  onNewConversation?: () => void;
  topbar?: React.ReactNode;
}

export function Shell({
  children,
  active,
  conversations = [],
  currentConversationId,
  onSelectConversation,
  onNewConversation,
  topbar,
}: ShellProps) {
  const { user, loading, login, logout } = useSession();

  if (loading) {
    return <div className="empty">Loading…</div>;
  }
  if (!user) {
    return <LoginGate onLogin={login} />;
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <header>
          <div className="brand">
            Salesforce AI Agent
            <small>Engineering console</small>
          </div>
        </header>
        <nav className="nav">
          <a href="/" className={active === "chat" ? "active" : ""}>
            Conversations
          </a>
          <a href="/connections" className={active === "connections" ? "active" : ""}>
            Salesforce connection
          </a>
          <a href="/releases" className={active === "releases" ? "active" : ""}>
            Releases
          </a>
          <a href="/audit" className={active === "audit" ? "active" : ""}>
            Audit history
          </a>
          <a href="/tools" className={active === "tools" ? "active" : ""}>
            Capabilities
          </a>
          <a href="/ai" className={active === "ai" ? "active" : ""}>
            AI providers
          </a>
          <a href="/settings" className={active === "settings" ? "active" : ""}>
            Project settings
          </a>
        </nav>
        <ProjectSwitcher current={user.project_id} />
        {active === "chat" && (
          <>
            <div style={{ padding: "10px 10px 0" }}>
              <button style={{ width: "100%" }} onClick={onNewConversation}>
                + New conversation
              </button>
            </div>
            <div className="conv-list">
              {conversations.map((c) => (
                <div
                  key={c.id}
                  className={`conv-item ${c.id === currentConversationId ? "active" : ""}`}
                  onClick={() => onSelectConversation?.(c.id)}
                >
                  {c.title}
                </div>
              ))}
              {!conversations.length && (
                <div className="muted" style={{ padding: 10, fontSize: 12 }}>
                  No conversations yet.
                </div>
              )}
            </div>
          </>
        )}
        <footer>
          <div className="row-between">
            <span title={user.email}>
              {user.email}
              <br />
              <span className="muted" style={{ fontSize: 11 }}>
                {user.company_name} · {user.project_name} · {user.role}
              </span>
            </span>
            <button className="ghost" onClick={logout} style={{ padding: "2px 8px" }}>
              Sign out
            </button>
          </div>
        </footer>
      </aside>
      <main className="main">
        <div className="topbar">{topbar}</div>
        {children}
      </main>
    </div>
  );
}

export function useSession() {
  const [user, setUser] = useState<UserOut | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .me()
      .then(setUser)
      .catch((err) => {
        if (!(err instanceof ApiError) || err.status !== 401) console.error(err);
      })
      .finally(() => setLoading(false));
  }, []);

  return {
    user,
    loading,
    login: async (email: string) => setUser(await api.login(email)),
    logout: async () => {
      await api.logout();
      setUser(null);
    },
  };
}

/**
 * The project is the security boundary, so which one the session acts in is
 * explicit and always visible: every query the backend runs is scoped to it,
 * and the user should never have to guess whose data they are looking at.
 */
function ProjectSwitcher({ current }: { current: string }) {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);

  useEffect(() => {
    api
      .projects()
      .then((r) => setProjects(r.projects))
      .catch(() => setProjects([]));
  }, []);

  if (projects.length < 2) return null;
  return (
    <div style={{ padding: "0 10px" }}>
      <select
        value={current}
        onChange={async (e) => {
          await api.switchProject(e.target.value);
          window.location.reload();
        }}
        style={{ width: "100%" }}
      >
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.company_name} / {p.name} ({p.role})
          </option>
        ))}
      </select>
    </div>
  );
}

function LoginGate({ onLogin }: { onLogin: (email: string) => Promise<void> }) {
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  return (
    <div className="gate">
      <div>
        <div className="card">
          <h3>Sign in</h3>
          <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
            Local identity for this deployment. If OIDC single sign-on is configured,
            use your identity provider instead.
          </p>
          <form
            className="stack"
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              setError(null);
              try {
                await onLogin(email);
              } catch (err) {
                setError(err instanceof Error ? err.message : "Sign-in failed");
              } finally {
                setBusy(false);
              }
            }}
          >
            <input
              type="email"
              required
              placeholder="you@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            <button className="primary" disabled={busy || !email}>
              {busy ? "Signing in…" : "Continue"}
            </button>
          </form>
          {error && (
            <div className="banner error" style={{ marginTop: 12 }}>
              {error}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
