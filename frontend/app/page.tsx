"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApprovalCard } from "@/components/ApprovalCard";
import { Shell } from "@/components/Shell";
import { Trace } from "@/components/Trace";
import { api } from "@/lib/api";
import { followRun, type AgentEvent } from "@/lib/sse";
import type {
  Approval,
  ChatMessage,
  Connection,
  Conversation,
  RiskLevel,
  TraceEntry,
} from "@/lib/types";

let seq = 0;
const nextId = () => `t${++seq}`;

export default function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [trace, setTrace] = useState<TraceEntry[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The run being followed, if any. Survives the component, not the tab. */
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const following = useRef<AbortController | null>(null);

  const connection = connections.find(
    (c) => c.id === conversations.find((x) => x.id === currentId)?.salesforce_connection_id,
  );

  const refreshConversations = useCallback(async () => {
    const list = await api.conversations();
    setConversations(list);
    return list;
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const [convs, conns] = await Promise.all([api.conversations(), api.connections()]);
        setConversations(convs);
        setConnections(conns);
        if (convs.length) setCurrentId(convs[0].id);
      } catch {
        /* unauthenticated — Shell renders the login gate */
      }
    })();
  }, []);

  useEffect(() => {
    if (!currentId) return;
    (async () => {
      const detail = await api.conversation(currentId);
      setMessages(detail.messages);
      setTrace([]);
      const { approvals: list } = await api.approvals(currentId);
      setApprovals(list.filter((a) => a.state === "PENDING"));

      // Work does not stop when a tab closes, so opening a conversation has to
      // ask whether something is still running rather than assume it is idle.
      const { active } = await api.conversationRuns(currentId);
      if (active.length) {
        setActiveRunId(active[0]);
        setRunning(true);
      }
    })();
  }, [currentId]);

  // Follow whichever run is live. Replays from the beginning of that run, so a
  // reload mid-run shows the steps that already happened rather than a gap.
  useEffect(() => {
    if (!activeRunId) return;
    const controller = new AbortController();
    following.current?.abort();
    following.current = controller;

    (async () => {
      try {
        await followRun(activeRunId, handleEvent, { signal: controller.signal });
      } finally {
        if (!controller.signal.aborted) {
          setRunning(false);
          setActiveRunId(null);
          if (currentId) {
            await reloadMessages(currentId);
            const { approvals: list } = await api.approvals(currentId);
            setApprovals(list.filter((a) => a.state === "PENDING"));
          }
        }
      }
    })();

    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRunId]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, trace, approvals]);

  const push = (entry: Omit<TraceEntry, "id" | "at">) =>
    setTrace((prev) => [...prev, { ...entry, id: nextId(), at: Date.now() }]);

  const handleEvent = useCallback((ev: AgentEvent) => {
    const d = ev.data as Record<string, any>;
    switch (ev.type) {
      case "run.started":
        push({ kind: "info", label: "Run started", detail: String(d.model ?? ""), status: "running" });
        break;
      case "state":
        push({ kind: "state", label: humanState(String(d.state)), status: "running" });
        break;
      case "tool.started":
        push({
          kind: "tool",
          label: describeTool(String(d.tool), d.arguments),
          status: "running",
          risk: d.risk as RiskLevel,
        });
        break;
      case "tool.verifying":
        push({ kind: "tool", label: "Verifying the change in Salesforce…", status: "running" });
        break;
      case "tool.finished":
        push({
          kind: "tool",
          label: `${d.tool} succeeded`,
          detail: summarize(d.summary),
          status: "ok",
        });
        break;
      case "tool.failed":
        push({
          kind: "tool",
          label: `${d.tool ?? "tool"} failed`,
          detail: String(d.error ?? summarize(d.summary)),
          status: "failed",
        });
        break;
      case "tool.blocked":
        push({ kind: "tool", label: `${d.tool} blocked by policy`, detail: String(d.reason), status: "blocked" });
        break;
      case "tool.idempotent_hit":
        push({ kind: "info", label: `${d.tool}: already applied in this run`, status: "ok" });
        break;
      case "tools.loaded":
        push({
          kind: "info",
          label: `${d.external} external tool(s) available`,
          detail: (d.providers ?? []).join(", "),
        });
        break;
      case "knowledge.recalled":
        push({
          kind: "info",
          label: `Recalled ${d.count} prior observation(s) about this org`,
        });
        break;
      case "deployment.started":
        push({
          kind: "deployment",
          label: d.check_only
            ? `Validating ${d.label ?? "metadata"} (nothing is saved)…`
            : `Deploying ${d.label ?? "metadata"}…`,
          detail: d.components ? `${d.components} component(s)` : undefined,
          status: "running",
        });
        break;
      case "deployment.finished":
        push({
          kind: "deployment",
          label: `Deployment ${String(d.status).toLowerCase()}`,
          detail: `${d.components_failed ?? 0} component error(s) · ${d.tests_failed ?? 0} test failure(s)`,
          status: d.success ? "ok" : "failed",
        });
        break;
      case "deployment.failed":
        push({ kind: "deployment", label: "Deployment failed", detail: String(d.error), status: "failed" });
        break;
      case "changeset.validating":
        push({
          kind: "deployment",
          label: "Validating change set against the org…",
          detail: `${d.components} component(s)`,
          status: "running",
        });
        break;
      case "changeset.rollback_delete":
        push({
          kind: "deployment",
          label: `Removing ${d.components} component(s) created by the deployment…`,
          status: "running",
        });
        break;
      case "apex.tests_started":
        push({
          kind: "tool",
          label: "Running Apex tests…",
          detail: (d.classes ?? []).join(", ") || String(d.level ?? ""),
          status: "running",
        });
        break;
      case "apex.tests_finished":
        push({
          kind: "tool",
          label: `Apex tests ${d.methods_failed ? "failed" : "passed"}`,
          detail: `${d.methods_run} method(s), ${d.methods_failed} failed`,
          status: d.methods_failed ? "failed" : "ok",
        });
        break;
      case "analysis.bulk_extract":
        push({
          kind: "info",
          label: `Extracting ${Number(d.estimated_records).toLocaleString()} ${d.object} records via the Bulk API…`,
          detail: "Analysis runs server-side; records do not enter the conversation.",
          status: "running",
        });
        break;
      case "analysis.dependencies":
        push({ kind: "tool", label: `Checking what depends on ${d.target}…`, status: "running" });
        break;
      case "debug.collecting":
        push({
          kind: "tool",
          label: `Collecting evidence from ${d.object}${d.record_id ? ` (${d.record_id})` : ""}…`,
          status: "running",
        });
        break;
      case "debug.diagnosing":
        push({
          kind: "tool",
          label: `Diagnosing from ${d.observations} observation(s)…`,
          status: "running",
        });
        break;
      case "bulk.started":
        push({
          kind: "deployment",
          label: `Bulk update of ${Number(d.records).toLocaleString()} ${d.object} record(s)…`,
          status: "running",
        });
        break;
      case "bulk.progress":
        push({
          kind: "deployment",
          label: `Bulk job ${d.state}`,
          detail: `${d.processed} processed · ${d.failed} failed`,
          status: "running",
        });
        break;
      case "bulk.finished":
        push({
          kind: "deployment",
          label: `Bulk job ${String(d.state).toLowerCase()}`,
          detail: `${d.processed} processed · ${d.failed} failed`,
          status: d.failed ? "failed" : "ok",
        });
        break;
      case "mcp.call_started":
        push({
          kind: "tool",
          label: `Calling ${d.tool} on external server '${d.server}'…`,
          status: "running",
        });
        break;
      case "approval.requested":
        push({ kind: "approval", label: "Waiting for human approval", status: "waiting" });
        setApprovals((prev) => [
          ...prev,
          {
            id: String(d.approval_id),
            agent_run_id: String(d.run_id ?? ""),
            conversation_id: "",
            tool_name: String(d.tool),
            risk_level: d.risk as RiskLevel,
            state: "PENDING",
            arguments: null,
            plan: { ...(d.plan ?? {}), risk: { risk: d.risk, reasons: d.reasons ?? [], requires_approval: true } },
            decision_note: null,
            created_at: new Date().toISOString(),
          },
        ]);
        break;
      case "approval.approved":
        push({ kind: "approval", label: `Approved — executing ${d.tool}`, status: "ok" });
        break;
      case "approval.rejected":
        push({ kind: "approval", label: "Rejected — no change was made", status: "blocked" });
        break;
      case "approval.invalidated":
        push({
          kind: "approval",
          label: "Approval no longer valid — nothing was executed",
          detail: String(d.reason ?? ""),
          status: "blocked",
        });
        break;
      case "assistant.text":
        break;
      case "run.paused":
        setApprovals((prev) =>
          prev.map((a) => (a.agent_run_id ? a : { ...a, agent_run_id: String(d.run_id) })),
        );
        break;
      case "run.halted":
        push({ kind: "error", label: String(d.message ?? "Run halted"), status: "failed" });
        break;
      case "run.cancelled":
        push({
          kind: "info",
          label: "Run cancelled",
          detail: String(d.message ?? ""),
          status: "blocked",
        });
        break;
      case "run.expired":
        push({
          kind: "error",
          label: "Run stopped — time limit reached",
          detail: String(d.message ?? ""),
          status: "failed",
        });
        break;
      case "run.completed":
        push({
          kind: "info",
          label: "Run completed",
          detail: `${d.steps} steps · ${d.input_tokens}/${d.output_tokens} tokens · ${Math.round(Number(d.duration_ms))}ms`,
          status: "ok",
        });
        break;
      case "error":
        setError(String(d.message));
        push({ kind: "error", label: String(d.message), status: "failed" });
        break;
      default:
        break;
    }
  }, []);

  async function reloadMessages(conversationId: string) {
    const detail = await api.conversation(conversationId);
    setMessages(detail.messages);
    await refreshConversations();
  }

  async function send() {
    const text = input.trim();
    if (!text || running) return;
    let conversationId = currentId;
    if (!conversationId) {
      const created = await api.createConversation(connections[0]?.id ?? null);
      await refreshConversations();
      setCurrentId(created.id);
      conversationId = created.id;
    }
    setInput("");
    setError(null);
    setRunning(true);
    setMessages((prev) => [
      ...prev,
      {
        id: `local-${Date.now()}`,
        role: "user",
        text,
        agent_run_id: null,
        created_at: new Date().toISOString(),
      },
    ]);
    try {
      // The post only queues the work. Following its timeline is a separate
      // step, which is what makes closing this tab harmless.
      const accepted = await api.sendMessage(conversationId, text);
      setActiveRunId(accepted.run_id);
    } catch (err) {
      setRunning(false);
      setError(err instanceof Error ? err.message : "Could not start the run");
    }
  }

  async function cancel() {
    if (!activeRunId) return;
    try {
      const result = await api.cancelRun(activeRunId);
      if (!result.success) setError(result.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not cancel the run");
    }
  }

  async function decide(
    id: string,
    decision: "approve" | "reject",
    note?: string,
    modified?: Record<string, unknown>,
  ) {
    const result = await api.decide(id, decision, note, modified);
    setApprovals((prev) => prev.filter((a) => a.id !== id));
    if (result.resume_ready) {
      // Resume is the same durability contract as the first message: queue it,
      // let a worker do it, follow the log.
      const resumed = await api.resumeRun(result.run_id);
      setRunning(true);
      setActiveRunId(resumed.run_id);
    }
  }

  return (
    <Shell
      active="chat"
      conversations={conversations}
      currentConversationId={currentId}
      onSelectConversation={setCurrentId}
      onNewConversation={async () => {
        const created = await api.createConversation(connections[0]?.id ?? null);
        await refreshConversations();
        setCurrentId(created.id);
        setMessages([]);
        setTrace([]);
        setApprovals([]);
      }}
      topbar={
        <>
          <strong>{conversations.find((c) => c.id === currentId)?.title ?? "Agent"}</strong>
          <div className="row-between" style={{ gap: 8 }}>
            {connection ? (
              <>
                <span className="muted">{connection.label || connection.username}</span>
                {/* Which environment a change lands in is the single most
                    consequential fact on this screen, so it is always visible
                    and PRODUCTION is filled rather than outlined. */}
                <span className={`badge ${connection.environment}`}>
                  {connection.environment}
                </span>
              </>
            ) : (
              <a href="/connections">Connect a Salesforce org →</a>
            )}
          </div>
        </>
      }
    >
      <div className="content">
        {!connections.length && (
          <div className="page">
            <div className="banner">
              No Salesforce org is connected. The agent can talk, but every Salesforce
              tool will fail until you <a href="/connections">connect an org</a>.
            </div>
          </div>
        )}
        {error && (
          <div className="page">
            <div className="banner error">{error}</div>
          </div>
        )}
        {!messages.length && !trace.length && (
          <div className="empty">
            <p>Ask the agent to inspect, query or change your Salesforce org.</p>
            <div className="stack" style={{ maxWidth: 420, margin: "0 auto" }}>
              {[
                "Inspect the Account object.",
                "Find the 10 newest Accounts.",
                "Create a Customer Tier picklist on Account with Enterprise, SMB and Startup.",
              ].map((s) => (
                <button key={s} className="ghost" onClick={() => setInput(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m) => (
          <div className={`msg ${m.role}`} key={m.id}>
            <div className="who">{m.role === "user" ? "You" : "Agent"}</div>
            <div className="body">{m.text}</div>
          </div>
        ))}

        <Trace entries={trace} />

        {approvals.map((a) => (
          <ApprovalCard key={a.id} approval={a} onDecide={decide} />
        ))}
        <div ref={bottom} />
      </div>

      <div className="composer">
        <div className="row">
          <textarea
            placeholder="Describe what you want to do in Salesforce…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
          />
          {running ? (
            <button className="ghost" onClick={() => void cancel()}>
              Stop
            </button>
          ) : null}
          <button className="primary" disabled={running || !input.trim()} onClick={() => void send()}>
            {running ? "Working…" : "Send"}
          </button>
        </div>
        {running && (
          <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            This is running on the server. You can close this tab — the work
            continues, and reopening the conversation picks it back up.
          </div>
        )}
      </div>
    </Shell>
  );
}

function humanState(state: string): string {
  const map: Record<string, string> = {
    CREATED: "Created",
    QUEUED: "Queued — waiting for a worker…",
    INSPECTING: "Inspecting Salesforce…",
    PLANNING: "Planning the next step…",
    WAITING_FOR_APPROVAL: "Waiting for approval",
    EXECUTING: "Executing…",
    VERIFYING: "Verifying the result…",
    COMPLETED: "Completed",
    FAILED: "Failed",
    CANCELLED: "Cancelled",
    EXPIRED: "Stopped — time limit reached",
  };
  return map[state] ?? state;
}

function describeTool(tool: string, args: unknown): string {
  const a = (args ?? {}) as Record<string, any>;
  switch (tool) {
    case "describe_object":
      return `Inspecting ${a.object} schema…`;
    case "query_salesforce":
      return "Querying Salesforce…";
    case "create_record":
      return `Preparing to create a ${a.object} record…`;
    case "update_record":
      return `Preparing to update ${a.object} ${a.record_id}…`;
    case "create_field":
      return `Preparing field ${a.api_name} on ${a.object}…`;
    case "deploy_metadata":
      return a.check_only === false ? "Deploying metadata…" : "Validating metadata…";
    case "list_flows":
      return a.object ? `Listing flows on ${a.object}…` : "Listing flows…";
    case "inspect_flow":
      return `Reading flow ${a.api_name}…`;
    case "create_flow":
      return `Designing flow ${a.api_name} on ${a.object}…`;
    case "update_flow":
      return `Preparing a new version of ${a.api_name}…`;
    case "activate_flow":
      return `Preparing to activate ${a.api_name}…`;
    case "deactivate_flow":
      return `Preparing to deactivate ${a.api_name}…`;
    case "validate_flow":
      return `Validating flow ${a.api_name} against the org…`;
    case "list_apex":
      return "Listing Apex classes and triggers…";
    case "inspect_apex":
      return `Reading Apex ${a.name}…`;
    case "write_apex":
      return `Preparing Apex ${a.kind ?? "class"} ${a.name}…`;
    case "validate_apex":
      return `Compiling ${a.name} against the org…`;
    case "run_apex_tests":
      return "Running Apex tests…";
    case "find_duplicates":
      return `Scanning ${a.object} for duplicates…`;
    case "analyze_data_quality":
      return `Analyzing ${a.object} data quality…`;
    case "prepare_merge_plan":
      return "Building a merge plan…";
    case "bulk_update":
      return `Preparing a bulk update of ${a.object}…`;
    case "analyze_dependencies":
      return `Checking dependencies of ${a.name ?? a.object}…`;
    case "debug_org_behaviour":
      return `Investigating ${a.object}…`;
    case "inspect_permissions":
      return "Resolving effective permissions…";
    case "audit_security":
      return "Auditing the org's access posture…";
    case "modify_permissions":
      return `Preparing a permission change for ${a.user_id}…`;
    case "list_reports":
    case "list_report_types":
      return "Listing reports…";
    case "inspect_report":
      return "Reading the report definition…";
    case "create_report":
      return `Preparing report '${a.name}'…`;
    case "inspect_dashboard":
      return "Reading dashboards…";
    case "create_change_set":
      return `Assembling change set '${a.name}'…`;
    case "validate_change_set":
      return "Validating the change set against the org…";
    case "deploy_change_set":
      return "Preparing to deploy the change set…";
    case "rollback_change_set":
      return "Preparing a rollback…";
    case "recall_org_knowledge":
      return "Recalling what we know about this org…";
    case "remember_about_org":
      return "Recording a fact about this org…";
    default:
      if (tool.startsWith("mcp__")) {
        const [, server, name] = tool.split("__");
        return `Running ${name} on external server '${server}'…`;
      }
      return `Running ${tool}…`;
  }
}

function summarize(summary: unknown): string {
  if (!summary || typeof summary !== "object") return "";
  const s = summary as Record<string, unknown>;
  const parts: string[] = [];
  if (s.object) parts.push(String(s.object));
  if (s.count !== undefined) parts.push(`${s.count} records`);
  if (s.record_id) parts.push(String(s.record_id));
  if (s.field) parts.push(String(s.field));
  if (s.deploy_id) parts.push(`deploy ${s.deploy_id}`);
  if (s.verified !== undefined) parts.push(s.verified ? "verified" : "unverified");
  return parts.join(" · ");
}
