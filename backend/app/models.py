"""Persistence model.

Naming, because this product has two things called "org" and confusing them is
how a tenant boundary gets crossed:

  * **Company**  — the paying customer. The top-level tenant.
  * **Project**  — a unit of work inside a company (Sales Cloud, Service Cloud,
    a migration). **The security boundary.** A Salesforce connection, the users
    who may use it, the policies, the AI configuration and the integrations all
    belong to a Project.
  * **SalesforceConnection** — an authenticated connection to a *Salesforce*
    org. Never called "Organization" anywhere in this codebase.

Every row that can reach customer data carries `company_id` **and**
`project_id`. Queries filter on `project_id`; `company_id` exists so a company
administrator can see across their own projects and never beyond.

Retention: this schema deliberately stores identity, security and governance
metadata — not customer content. Salesforce records, Jira descriptions, repo
contents, uploaded documents and model prompts are processed and discarded
unless a customer explicitly enables retention.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.observability.logging import new_id


def utcnow() -> datetime:
    return datetime.now(UTC)


# ===========================================================================
# Enumerations
# ===========================================================================
class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ApprovalState(str, enum.Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ExecutionState(str, enum.Enum):
    """State of one *tool* execution inside a run."""

    PLANNED = "PLANNED"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    PARTIALLY_SUCCEEDED = "PARTIALLY_SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class RunState(str, enum.Enum):
    """State of an agent run. Owned by the server, never by the browser.

    Transitions are deterministic; see app/execution/state.py for the legal
    transition table. There is no ambiguous state and no state that means
    "the user closed the tab".
    """

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    INSPECTING = "INSPECTING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


TERMINAL_RUN_STATES = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.EXPIRED}
)


class CompanyRole(str, enum.Enum):
    """Authority over the company itself: billing, projects, company-wide SSO."""

    PLATFORM_OWNER = "PLATFORM_OWNER"   # operates this deployment
    COMPANY_ADMIN = "COMPANY_ADMIN"     # owns the customer account
    COMPANY_MEMBER = "COMPANY_MEMBER"   # belongs to the company, no company-wide power
    COMPANY_AUDITOR = "COMPANY_AUDITOR" # reads audit across the company, changes nothing


class ProjectRole(str, enum.Enum):
    """Authority inside one project.

    Deliberately separate from Salesforce authority: PROJECT_ADMIN manages the
    project (members, integrations, policy) and does **not** thereby become the
    agent's Salesforce superuser. What the agent may do in Salesforce is decided
    by policy plus the connected Salesforce user's own permissions.
    """

    PROJECT_ADMIN = "PROJECT_ADMIN"
    SALESFORCE_ADMIN = "SALESFORCE_ADMIN"
    RELEASE_MANAGER = "RELEASE_MANAGER"
    SECURITY_ADMIN = "SECURITY_ADMIN"
    DEVELOPER = "DEVELOPER"
    USER = "USER"
    AUDITOR = "AUDITOR"
    VIEWER = "VIEWER"


#: Coarse ranking for "at least this much authority" checks. Approval
#: *eligibility* is decided by explicit role sets in app/tenancy/policy.py,
#: never by this ranking.
PROJECT_ROLE_RANK: dict[ProjectRole, int] = {
    ProjectRole.VIEWER: 10,
    ProjectRole.AUDITOR: 15,
    ProjectRole.USER: 20,
    ProjectRole.DEVELOPER: 30,
    ProjectRole.SECURITY_ADMIN: 40,
    ProjectRole.RELEASE_MANAGER: 40,
    ProjectRole.SALESFORCE_ADMIN: 45,
    ProjectRole.PROJECT_ADMIN: 50,
}


class Environment(str, enum.Enum):
    """Which Salesforce environment a connection represents.

    Drives policy: the same change is routine in DEV and a governed release in
    PRODUCTION.
    """

    DEVELOPMENT = "DEVELOPMENT"
    SANDBOX = "SANDBOX"
    UAT = "UAT"
    PRODUCTION = "PRODUCTION"


#: Default promotion path. A project admin may narrow it, never widen it past
#: PRODUCTION being last.
DEFAULT_PROMOTION_PATH = [
    Environment.DEVELOPMENT,
    Environment.SANDBOX,
    Environment.UAT,
    Environment.PRODUCTION,
]


class IdentityProviderKind(str, enum.Enum):
    LOCAL = "LOCAL"
    OIDC = "OIDC"
    SAML = "SAML"


class PlanTier(str, enum.Enum):
    """Commercial tier. Entitlements are enforced; payment is not implemented."""

    TRIAL = "TRIAL"
    STARTER = "STARTER"
    BUSINESS = "BUSINESS"
    ENTERPRISE = "ENTERPRISE"


# ===========================================================================
# Tenancy
# ===========================================================================
class Company(Base):
    """The customer. Top of the tenancy tree."""

    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("co"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    plan: Mapped[PlanTier] = mapped_column(Enum(PlanTier), default=PlanTier.TRIAL)
    #: Email domains that may self-join via SSO. Empty means invitation only.
    sso_domains: Mapped[list | None] = mapped_column(JSON, nullable=True)

    #: Whether this customer has cleared the agent to change **their own**
    #: production orgs. Off until a company administrator turns it on.
    #:
    #: This layer exists because the deployment-wide ceiling is a single global
    #: switch, and in a multi-tenant deployment that is the wrong shape: either
    #: no customer can ever touch production, or every customer's production
    #: posture rests entirely on their own project settings. Neither is what an
    #: enterprise wants. One customer may be cleared for production while
    #: another is still in trial and must never reach it.
    #:
    #: It is a company-level decision, not a project one, because the person who
    #: owns the customer account is the one accountable for it — a project
    #: administrator running one team's workspace is not.
    allow_production_mutations: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    memberships: Mapped[list[CompanyMembership]] = relationship(back_populates="company")
    projects: Mapped[list[Project]] = relationship(back_populates="company")


class CompanyMembership(Base):
    """User ↔ company. Grants nothing inside a project on its own."""

    __tablename__ = "company_memberships"
    __table_args__ = (
        UniqueConstraint("company_id", "user_id", name="uq_company_membership"),
        Index("ix_company_membership_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("cmem"))
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[CompanyRole] = mapped_column(Enum(CompanyRole), default=CompanyRole.COMPANY_MEMBER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    company: Mapped[Company] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="company_memberships")


class Project(Base):
    """The security boundary.

    Everything that can reach a customer's Salesforce org, repository or issue
    tracker hangs off a project, and every query filters on `project_id`.
    """

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("company_id", "slug", name="uq_project_slug"),
        Index("ix_project_company", "company_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("prj"))
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Ordered Environment values a change may be promoted through.
    promotion_path: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    company: Mapped[Company] = relationship(back_populates="projects")
    memberships: Mapped[list[ProjectMembership]] = relationship(back_populates="project")


class ProjectMembership(Base):
    """User ↔ project, with a project role. Authorization resolves through here."""

    __tablename__ = "project_memberships"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_membership"),
        Index("ix_project_membership_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("pmem"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    #: Least privilege by default; the project creator is set explicitly.
    role: Mapped[ProjectRole] = mapped_column(Enum(ProjectRole), default=ProjectRole.VIEWER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    invited_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The identity provider's subject, when an IdP granted this membership.
    #: Its presence is what distinguishes a membership the directory granted —
    #: and may therefore revoke — from one a person granted by hand, which the
    #: directory has no business taking away.
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="project_memberships")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("usr"))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    idp_kind: Mapped[IdentityProviderKind] = mapped_column(
        Enum(IdentityProviderKind), default=IdentityProviderKind.LOCAL
    )
    idp_subject: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Bumped to invalidate every session this user holds, everywhere, at once.
    #: A session token carries the version it was minted at; a mismatch is a
    #: rejected session. This is what makes "sign out everywhere" and
    #: "revoke access now" real rather than best-effort.
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company_memberships: Mapped[list[CompanyMembership]] = relationship(back_populates="user")
    project_memberships: Mapped[list[ProjectMembership]] = relationship(back_populates="user")


class Invitation(Base):
    """A pending invitation into a project. Expires; single use."""

    __tablename__ = "invitations"
    __table_args__ = (Index("ix_invite_project", "project_id", "email"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("inv"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[ProjectRole] = mapped_column(Enum(ProjectRole), default=ProjectRole.VIEWER)
    #: Only the hash is stored; the token itself is shown once to the inviter.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    invited_by: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ===========================================================================
# Policy
# ===========================================================================
class ProjectPolicy(Base):
    """Per-project policy. Deterministic; the model never reads or writes it.

    A project policy may only be *stricter* than the deployment ceilings in
    app.config. It can never widen them.
    """

    __tablename__ = "project_policies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("pol"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), unique=True, index=True)

    allow_production_mutations: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_ttl_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    max_agent_steps: Mapped[int] = mapped_column(Integer, default=20)
    max_execution_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    max_tool_calls: Mapped[int] = mapped_column(Integer, default=120)
    require_separate_approver: Mapped[bool] = mapped_column(Boolean, default=False)
    approver_matrix: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    disabled_tools: Mapped[list | None] = mapped_column(JSON, nullable=True)
    max_bulk_records: Mapped[int] = mapped_column(Integer, default=50_000)

    #: Environments the agent may mutate at all, as Environment values.
    allowed_environments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    #: LLM providers permitted for this project. Empty means "any configured".
    allowed_llm_providers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    #: Silently switching provider on failure is a data-path change, so it is
    #: opt-in per project rather than a helpful default.
    allow_llm_fallback: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Retention, in days. 0 means "do not retain" — the default everywhere.
    retain_conversation_days: Mapped[int] = mapped_column(Integer, default=0)
    retain_tool_payload_days: Mapped[int] = mapped_column(Integer, default=0)
    retain_document_days: Mapped[int] = mapped_column(Integer, default=0)
    audit_retention_days: Mapped[int] = mapped_column(Integer, default=365)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ===========================================================================
# Salesforce
# ===========================================================================
class SalesforceConnection(Base):
    """An authenticated connection to one Salesforce org, owned by a project.

    Named for what it is. `Company` is the tenant; this is the Salesforce side.
    """

    __tablename__ = "salesforce_connections"
    __table_args__ = (
        UniqueConstraint("project_id", "sf_org_id", name="uq_project_sf_org"),
        Index("ix_sfconn_project", "project_id"),
        Index("ix_sfconn_company", "company_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sfc"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    #: Who completed the OAuth handshake. Provenance, not an access grant.
    connected_by: Mapped[str] = mapped_column(String(64), index=True)

    label: Mapped[str] = mapped_column(String(120), default="")
    environment: Mapped[Environment] = mapped_column(
        Enum(Environment), default=Environment.SANDBOX
    )

    sf_org_id: Mapped[str] = mapped_column(String(32), index=True)
    sf_user_id: Mapped[str] = mapped_column(String(32), default="")
    username: Mapped[str] = mapped_column(String(320), default="")
    instance_url: Mapped[str] = mapped_column(String(512))
    login_url: Mapped[str] = mapped_column(String(512), default="")
    is_sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    org_type: Mapped[str] = mapped_column(String(64), default="unknown")
    api_version: Mapped[str] = mapped_column(String(16), default="62.0")

    #: Which Salesforce app authorised this connection. Null means the
    #: deployment-managed one. A refresh must be presented to the same app that
    #: issued the token, so this is recorded rather than resolved again — a
    #: company switching apps must not silently break every existing
    #: connection's refresh.
    salesforce_app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Secret-store references, never the secrets themselves.
    access_token_ref: Mapped[str] = mapped_column(Text)
    refresh_token_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    token_fingerprint: Mapped[str] = mapped_column(String(32), default="")
    scopes: Mapped[str] = mapped_column(String(512), default="")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SalesforceApp(Base):
    """A Salesforce External Client App, owned by one customer company.

    The reason this table exists: a Connected App is not an implementation
    detail of the vendor, it is a security control belonging to the customer.
    Whoever owns it decides which profiles may use it, which IP ranges may
    reach it, how long a refresh token lives, and — the part that matters most
    — can revoke every session with it in one click.

    Holding one shared app in the deployment's environment variables makes that
    control ours instead of theirs. It also means every tenant shares a single
    revocation: one customer's security team pulling the app takes every other
    customer offline with it, and one org's API limits are visible to all.

    So a company may register its own. The consumer secret goes to the secret
    store like any other credential and only a fingerprint comes back out;
    there is no endpoint that returns it. A deployment-managed app remains as a
    fallback for self-serve signups, and can be switched off entirely.
    """

    __tablename__ = "salesforce_apps"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_sfapp_company_name"),
        Index("ix_sfapp_company", "company_id"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("sfapp")
    )
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    #: Null means the app serves every project in the company. A project-scoped
    #: app wins over a company-wide one, so one project can point at a
    #: different Salesforce app without disturbing the rest.
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(120), default="Salesforce app")
    #: The consumer key. Not a secret in the way the consumer secret is — it
    #: travels in every authorize URL — so it is shown in the UI, which is what
    #: makes a mismatch diagnosable.
    client_id: Mapped[str] = mapped_column(String(512), default="")
    #: A secret-store reference. Never the secret.
    client_secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    login_url: Mapped[str] = mapped_column(String(512), default="https://login.salesforce.com")
    api_version: Mapped[str] = mapped_column(String(16), default="")

    is_default: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class OAuthState(Base):
    """Short-lived CSRF/PKCE state for an OAuth handshake, for any provider."""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), default="salesforce")
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    code_verifier: Mapped[str] = mapped_column(String(256))
    login_url: Mapped[str] = mapped_column(String(512), default="")
    #: Which app began this handshake. The token exchange must present the same
    #: client_id the authorize URL did, so this cannot be re-resolved later —
    #: an admin editing the company's app mid-login would otherwise swap the
    #: credentials underneath an in-flight exchange.
    salesforce_app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    redirect_after: Mapped[str] = mapped_column(String(512), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ===========================================================================
# Conversations and runs
# ===========================================================================
class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conv_project_created", "project_id", "created_at"),
        Index("ix_conv_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("conv"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    salesforce_connection_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(300), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", order_by="Message.created_at"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_msg_conv_created", "conversation_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("msg"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text, default="")
    blocks: Mapped[list | None] = mapped_column(JSON, nullable=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AgentRun(Base):
    """One agent execution. Server-owned; the browser is only a viewer.

    A run's lifetime is not tied to any HTTP request. It is picked up by a
    worker, advances through `RunState`, and can be reconnected to at any point.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_run_project_created", "project_id", "created_at"),
        Index("ix_run_state", "state"),
        Index("ix_run_conv", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("run"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    salesforce_connection_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Correlates every log line, audit row and step for this run.
    correlation_id: Mapped[str] = mapped_column(
        String(64), index=True, default=lambda: new_id("cor")
    )

    state: Mapped[RunState] = mapped_column(Enum(RunState), default=RunState.CREATED)
    user_request: Mapped[str] = mapped_column(Text, default="")
    steps_used: Mapped[int] = mapped_column(Integer, default=0)
    max_steps: Mapped[int] = mapped_column(Integer, default=20)
    tool_calls_used: Mapped[int] = mapped_column(Integer, default=0)

    #: Which model actually served the run — provider, model and tier.
    llm_provider: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    model_tier: Mapped[str] = mapped_column(String(20), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    final_text: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)

    #: Model transcript, kept only while the run is live (or if the project
    #: policy retains conversations), so an interrupted run can resume.
    transcript: Mapped[list | None] = mapped_column(JSON, nullable=True)
    pending_tool_results: Mapped[list | None] = mapped_column(JSON, nullable=True)
    pending_approval_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    #: Worker coordination.
    claimed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set by the user; the worker checks it between steps.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RunEvent(Base):
    """One durable entry in a run's execution timeline.

    Persisted rather than streamed-and-forgotten, which is what makes the
    timeline survive a disconnect: a reconnecting client asks for everything
    after the last sequence number it saw.

    These are *execution events* — what the agent did. Never private reasoning.
    """

    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("agent_run_id", "sequence", name="uq_run_event_seq"),
        Index("ix_run_event_stream", "agent_run_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ev"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str] = mapped_column(String(64), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(60))
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolExecution(Base):
    __tablename__ = "tool_executions"
    __table_args__ = (
        Index("ix_tool_run", "agent_run_id", "created_at"),
        Index("ix_tool_project", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("tex"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    salesforce_connection_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    tool_provider: Mapped[str] = mapped_column(String(60), default="native")
    tool_use_id: Mapped[str] = mapped_column(String(128), default="")
    arguments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    risk_level: Mapped[RiskLevel] = mapped_column(Enum(RiskLevel), default=RiskLevel.LOW)
    approval_state: Mapped[ApprovalState] = mapped_column(
        Enum(ApprovalState), default=ApprovalState.NOT_REQUIRED
    )
    execution_state: Mapped[ExecutionState] = mapped_column(
        Enum(ExecutionState), default=ExecutionState.PLANNED
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    salesforce_object: Mapped[str | None] = mapped_column(String(128), nullable=True)
    record_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ===========================================================================
# Approval
# ===========================================================================
class Approval(Base):
    """Explicit human authorization. Never inferred from conversational text.

    An APPROVED row is not authority to execute. Before a mutation runs the
    runtime re-checks that the approval is inside its window, still describes
    the same operation (`change_hash`), and still matches the org state it was
    proposed against (`state_fingerprint`).
    """

    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_appr_project_state", "project_id", "state"),
        Index("ix_appr_user_state", "user_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("apr"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    #: The requester.
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    salesforce_connection_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    environment: Mapped[Environment | None] = mapped_column(Enum(Environment), nullable=True)

    tool_name: Mapped[str] = mapped_column(String(128))
    tool_use_id: Mapped[str] = mapped_column(String(128), default="")
    arguments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    risk_level: Mapped[RiskLevel] = mapped_column(Enum(RiskLevel), default=RiskLevel.MEDIUM)
    state: Mapped[ApprovalState] = mapped_column(Enum(ApprovalState), default=ApprovalState.PENDING)

    change_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    state_fingerprint: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    approvals_required: Mapped[int] = mapped_column(Integer, default=1)
    eligible_roles: Mapped[list | None] = mapped_column(JSON, nullable=True)
    require_separate_approver: Mapped[bool] = mapped_column(Boolean, default=False)

    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    modified_arguments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    decisions: Mapped[list[ApprovalDecision]] = relationship(
        back_populates="approval", order_by="ApprovalDecision.created_at"
    )

    def effective_arguments(self) -> dict:
        return self.modified_arguments or self.arguments or {}


class ApprovalDecision(Base):
    """One human's vote. Multi-approver policies count these."""

    __tablename__ = "approval_decisions"
    __table_args__ = (
        UniqueConstraint("approval_id", "user_id", name="uq_approval_voter"),
        Index("ix_appr_dec_approval", "approval_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("apd"))
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"), index=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[ProjectRole] = mapped_column(Enum(ProjectRole), default=ProjectRole.VIEWER)
    decision: Mapped[str] = mapped_column(String(16))  # 'approve' | 'reject'
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_change_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    approval: Mapped[Approval] = relationship(back_populates="decisions")


# ===========================================================================
# Deployment and release
# ===========================================================================
class ChangeSetState(str, enum.Enum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    DEPLOYING = "DEPLOYING"
    DEPLOYED = "DEPLOYED"
    DEPLOY_FAILED = "DEPLOY_FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class ChangeSet(Base):
    """A named bundle of metadata moving through the release lifecycle."""

    __tablename__ = "change_sets"
    __table_args__ = (Index("ix_cs_project_created", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("cs"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    salesforce_connection_id: Mapped[str] = mapped_column(String(64), index=True)
    environment: Mapped[Environment] = mapped_column(
        Enum(Environment), default=Environment.SANDBOX
    )
    agent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    release_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[ChangeSetState] = mapped_column(
        Enum(ChangeSetState), default=ChangeSetState.DRAFT
    )
    source_files: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    package_manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    diff: Mapped[list | None] = mapped_column(JSON, nullable=True)
    test_level: Mapped[str] = mapped_column(String(32), default="RunLocalTests")
    run_tests: Mapped[list | None] = mapped_column(JSON, nullable=True)

    validation_deploy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    validation_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    deploy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deploy_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rollback_plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rolled_back_deploy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ReleaseState(str, enum.Enum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    DEPLOYING = "DEPLOYING"
    RELEASED = "RELEASED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class Release(Base):
    """A promotion of one or more change sets into a target environment."""

    __tablename__ = "releases"
    __table_args__ = (Index("ix_release_project", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("rel"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    source_environment: Mapped[Environment | None] = mapped_column(
        Enum(Environment), nullable=True
    )
    target_environment: Mapped[Environment] = mapped_column(Enum(Environment))
    state: Mapped[ReleaseState] = mapped_column(Enum(ReleaseState), default=ReleaseState.DRAFT)
    change_set_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (Index("ix_deploy_project", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("dep"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    change_set_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    salesforce_connection_id: Mapped[str] = mapped_column(String(64), index=True)
    environment: Mapped[Environment] = mapped_column(
        Enum(Environment), default=Environment.SANDBOX
    )

    salesforce_deploy_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    check_only: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(64), default="Queued")
    components_total: Mapped[int] = mapped_column(Integer, default=0)
    components_failed: Mapped[int] = mapped_column(Integer, default=0)
    tests_total: Mapped[int] = mapped_column(Integer, default=0)
    tests_failed: Mapped[int] = mapped_column(Integer, default=0)
    package_manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    errors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ===========================================================================
# Audit
# ===========================================================================
class AuditEvent(Base):
    """Enterprise audit trail.

    Deliberately metadata, not content: ids, hashes, counts, outcomes. Payloads
    are redacted and truncated before they land here, and customer content is
    not retained by default.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_project_created", "project_id", "created_at"),
        Index("ix_audit_company_created", "company_id", "created_at"),
        Index("ix_audit_action", "action"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("aud"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor_type: Mapped[str] = mapped_column(String(24), default="user")  # user|agent|system|scim

    salesforce_connection_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sf_org_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    environment: Mapped[Environment | None] = mapped_column(Enum(Environment), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tool_execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    action: Mapped[str] = mapped_column(String(128), index=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    arguments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    salesforce_object: Mapped[str | None] = mapped_column(String(128), nullable=True)
    record_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    before_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    risk_level: Mapped[RiskLevel | None] = mapped_column(Enum(RiskLevel), nullable=True)
    approval_state: Mapped[ApprovalState | None] = mapped_column(
        Enum(ApprovalState), nullable=True
    )
    execution_state: Mapped[ExecutionState | None] = mapped_column(
        Enum(ExecutionState), nullable=True
    )
    deployment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(64), default="ok")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ===========================================================================
# AI providers
# ===========================================================================
class LLMProviderKind(str, enum.Enum):
    ANTHROPIC = "ANTHROPIC"
    OPENAI = "OPENAI"
    AZURE_OPENAI = "AZURE_OPENAI"
    BEDROCK = "BEDROCK"
    GOOGLE = "GOOGLE"
    VERTEX = "VERTEX"
    MISTRAL = "MISTRAL"
    GROQ = "GROQ"
    DEEPSEEK = "DEEPSEEK"
    TOGETHER = "TOGETHER"
    OLLAMA = "OLLAMA"
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"


class ModelTier(str, enum.Enum):
    FAST = "FAST"
    BALANCED = "BALANCED"
    ADVANCED = "ADVANCED"


class LLMCredential(Base):
    """A customer's own AI provider credential (BYOK), per project.

    The secret itself never lives here — only a reference into the secret
    store — and it is never returned to a browser after submission.
    """

    __tablename__ = "llm_credentials"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_llm_credential_name"),
        Index("ix_llmcred_project", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("llm"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)

    name: Mapped[str] = mapped_column(String(80))
    provider: Mapped[LLMProviderKind] = mapped_column(Enum(LLMProviderKind))
    #: Reference into the secret store (e.g. "local:...", "awskms:...").
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Non-secret provider settings: deployment name, project id, org id.
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    #: Model chosen for each tier: {"FAST": "...", "BALANCED": "...", ...}
    tier_models: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class LLMUsage(Base):
    """Per-run model usage. Counts and cost estimates, never prompt content."""

    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llmusage_project_created", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("use"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    credential_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(128))
    tier: Mapped[str] = mapped_column(String(20), default="")
    requests: Mapped[int] = mapped_column(Integer, default=1)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    byok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ===========================================================================
# Integrations
# ===========================================================================
class IntegrationKind(str, enum.Enum):
    JIRA = "JIRA"
    GITHUB = "GITHUB"
    BITBUCKET = "BITBUCKET"
    SLACK = "SLACK"
    TEAMS = "TEAMS"


class IntegrationConnection(Base):
    """A non-Salesforce system connected to one project."""

    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint("project_id", "kind", "account", name="uq_integration"),
        Index("ix_integration_project", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("int"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[IntegrationKind] = mapped_column(Enum(IntegrationKind), index=True)

    #: Workspace / org / site identifier at the provider.
    account: Mapped[str] = mapped_column(String(200), default="")
    display_name: Mapped[str] = mapped_column(String(200), default="")
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    access_token_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scopes: Mapped[str] = mapped_column(String(512), default="")
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    connected_by: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Repository(Base):
    """A source repository bound to a project, on GitHub or Bitbucket."""

    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("project_id", "provider", "full_name", name="uq_repository"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("repo"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    integration_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[IntegrationKind] = mapped_column(Enum(IntegrationKind))

    full_name: Mapped[str] = mapped_column(String(300))
    default_branch: Mapped[str] = mapped_column(String(200), default="main")
    #: Branch-name patterns the agent may create or push to.
    allowed_branch_patterns: Mapped[list | None] = mapped_column(JSON, nullable=True)
    #: Path prefix inside the repo the agent may modify (e.g. force-app/).
    allowed_paths: Mapped[list | None] = mapped_column(JSON, nullable=True)
    require_pull_request: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class McpServerConfig(Base):
    """An MCP server this project may use as an additional tool provider.

    MCP is a transport, not a trust boundary: every tool discovered here still
    passes through the same risk engine, approval gate, audit and verification.
    """

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_mcp_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("mcp"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    transport: Mapped[str] = mapped_column(String(16), default="stdio")  # stdio | http
    command: Mapped[str | None] = mapped_column(String(512), nullable=True)
    args: Mapped[list | None] = mapped_column(JSON, nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    env: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tool_allowlist: Mapped[list | None] = mapped_column(JSON, nullable=True)
    risk_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    discovered_tools: Mapped[list | None] = mapped_column(JSON, nullable=True)
    last_discovery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ===========================================================================
# Data operations and knowledge
# ===========================================================================
class DataJob(Base):
    """A large-scale data operation executed through the Bulk API."""

    __tablename__ = "data_jobs"
    __table_args__ = (Index("ix_datajob_project", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("job"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    salesforce_connection_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    operation: Mapped[str] = mapped_column(String(32))
    sobject: Mapped[str] = mapped_column(String(128))
    sf_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(32), default="Planned")
    records_total: Mapped[int] = mapped_column(Integer, default=0)
    records_processed: Mapped[int] = mapped_column(Integer, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, default=0)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    failures_sample: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class KnowledgeKind(str, enum.Enum):
    OBJECT = "OBJECT"
    FIELD = "FIELD"
    AUTOMATION = "AUTOMATION"
    APEX = "APEX"
    REPORT = "REPORT"
    PERMISSION = "PERMISSION"
    DEPLOYMENT = "DEPLOYMENT"
    FAILURE = "FAILURE"
    PATTERN = "PATTERN"
    DIAGNOSIS = "DIAGNOSIS"


class OrgKnowledge(Base):
    """Accumulated knowledge of one connected Salesforce org.

    A retrieval index of small summaries, not a copy of the org, and never a
    place where the model's own claims become facts.
    """

    __tablename__ = "org_knowledge"
    __table_args__ = (
        UniqueConstraint(
            "salesforce_connection_id", "kind", "key", name="uq_knowledge"
        ),
        Index("ix_knowledge_lookup", "salesforce_connection_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("kno"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    salesforce_connection_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[KnowledgeKind] = mapped_column(Enum(KnowledgeKind))
    key: Mapped[str] = mapped_column(String(255), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="describe")
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ===========================================================================
# Identity configuration
# ===========================================================================
class SSOConfiguration(Base):
    """Per-company enterprise SSO. Secrets live in the secret store."""

    __tablename__ = "sso_configurations"
    __table_args__ = (UniqueConstraint("company_id", "kind", name="uq_sso_kind"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sso"))
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[IdentityProviderKind] = mapped_column(Enum(IdentityProviderKind))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    issuer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    client_secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    jwks_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    redirect_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    scopes: Mapped[str] = mapped_column(String(300), default="openid email profile")

    #: {"idp-group": "PROJECT_ROLE"} — how IdP groups become roles here.
    group_mappings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    default_project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_project_role: Mapped[ProjectRole] = mapped_column(
        Enum(ProjectRole), default=ProjectRole.VIEWER
    )

    scim_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    scim_token_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Subscription(Base):
    """Plan and entitlements.

    Entitlements are enforced. Payment collection is **not implemented** — see
    docs/operations.md. No billing provider is called anywhere in this codebase.
    """

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sub"))
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), unique=True, index=True)
    plan: Mapped[PlanTier] = mapped_column(Enum(PlanTier), default=PlanTier.TRIAL)
    status: Mapped[str] = mapped_column(String(32), default="active")
    max_projects: Mapped[int] = mapped_column(Integer, default=1)
    max_users: Mapped[int] = mapped_column(Integer, default=5)
    max_salesforce_connections: Mapped[int] = mapped_column(Integer, default=1)
    monthly_run_allowance: Mapped[int] = mapped_column(Integer, default=200)
    features: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: External billing reference, if a provider is ever connected.
    external_customer_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    current_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
