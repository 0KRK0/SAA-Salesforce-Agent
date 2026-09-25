"""Initial schema: company -> project tenancy

This replaces the pre-release initial migration outright rather than migrating
away from it. The `Organization`/`OrgConnection` schema it created was never
deployed anywhere, so there is no data to preserve and a rename chain would be
fiction. Any environment created against that earlier revision must be dropped
and recreated.

Naming, deliberately: `Company` is the paying customer, `Project` is the
security boundary, and `SalesforceConnection` is a connection to a Salesforce
org — never "Organization", which in a Salesforce product means two things.



Revision ID: 4f03de264731
Revises: 
Create Date: 2026-08-25 13:11:27.319081
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '4f03de264731'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('approvals',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=False),
    sa.Column('conversation_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('salesforce_connection_id', sa.String(length=64), nullable=True),
    sa.Column('environment', sa.Enum('DEVELOPMENT', 'SANDBOX', 'UAT', 'PRODUCTION', name='environment'), nullable=True),
    sa.Column('tool_name', sa.String(length=128), nullable=False),
    sa.Column('tool_use_id', sa.String(length=128), nullable=False),
    sa.Column('arguments', sa.JSON(), nullable=True),
    sa.Column('plan', sa.JSON(), nullable=True),
    sa.Column('risk_level', sa.Enum('LOW', 'MEDIUM', 'HIGH', 'CRITICAL', name='risklevel'), nullable=False),
    sa.Column('state', sa.Enum('NOT_REQUIRED', 'PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', name='approvalstate'), nullable=False),
    sa.Column('change_hash', sa.String(length=64), nullable=False),
    sa.Column('state_fingerprint', sa.JSON(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('invalidated_reason', sa.Text(), nullable=True),
    sa.Column('approvals_required', sa.Integer(), nullable=False),
    sa.Column('eligible_roles', sa.JSON(), nullable=True),
    sa.Column('require_separate_approver', sa.Boolean(), nullable=False),
    sa.Column('approved_by', sa.String(length=64), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_by', sa.String(length=64), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.Text(), nullable=True),
    sa.Column('modified_arguments', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('approvals', schema=None) as batch_op:
        batch_op.create_index('ix_appr_project_state', ['project_id', 'state'], unique=False)
        batch_op.create_index('ix_appr_user_state', ['user_id', 'state'], unique=False)
        batch_op.create_index(batch_op.f('ix_approvals_agent_run_id'), ['agent_run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_approvals_change_hash'), ['change_hash'], unique=False)
        batch_op.create_index(batch_op.f('ix_approvals_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_approvals_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_approvals_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_approvals_user_id'), ['user_id'], unique=False)

    op.create_table('audit_events',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=True),
    sa.Column('user_id', sa.String(length=64), nullable=True),
    sa.Column('actor_type', sa.String(length=24), nullable=False),
    sa.Column('salesforce_connection_id', sa.String(length=64), nullable=True),
    sa.Column('sf_org_id', sa.String(length=32), nullable=True),
    sa.Column('environment', sa.Enum('DEVELOPMENT', 'SANDBOX', 'UAT', 'PRODUCTION', name='environment'), nullable=True),
    sa.Column('conversation_id', sa.String(length=64), nullable=True),
    sa.Column('agent_run_id', sa.String(length=64), nullable=True),
    sa.Column('correlation_id', sa.String(length=64), nullable=True),
    sa.Column('tool_execution_id', sa.String(length=64), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('ip_hash', sa.String(length=64), nullable=True),
    sa.Column('action', sa.String(length=128), nullable=False),
    sa.Column('tool_name', sa.String(length=128), nullable=True),
    sa.Column('arguments', sa.JSON(), nullable=True),
    sa.Column('result_summary', sa.JSON(), nullable=True),
    sa.Column('salesforce_object', sa.String(length=128), nullable=True),
    sa.Column('record_ids', sa.JSON(), nullable=True),
    sa.Column('before_values', sa.JSON(), nullable=True),
    sa.Column('after_values', sa.JSON(), nullable=True),
    sa.Column('risk_level', sa.Enum('LOW', 'MEDIUM', 'HIGH', 'CRITICAL', name='risklevel'), nullable=True),
    sa.Column('approval_state', sa.Enum('NOT_REQUIRED', 'PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', name='approvalstate'), nullable=True),
    sa.Column('execution_state', sa.Enum('PLANNED', 'PROPOSED', 'APPROVED', 'EXECUTING', 'SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'FAILED', 'SKIPPED', name='executionstate'), nullable=True),
    sa.Column('deployment_id', sa.String(length=64), nullable=True),
    sa.Column('outcome', sa.String(length=64), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.create_index('ix_audit_action', ['action'], unique=False)
        batch_op.create_index('ix_audit_company_created', ['company_id', 'created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_action'), ['action'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_agent_run_id'), ['agent_run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_correlation_id'), ['correlation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_user_id'), ['user_id'], unique=False)
        batch_op.create_index('ix_audit_project_created', ['project_id', 'created_at'], unique=False)

    op.create_table('change_sets',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('salesforce_connection_id', sa.String(length=64), nullable=False),
    sa.Column('environment', sa.Enum('DEVELOPMENT', 'SANDBOX', 'UAT', 'PRODUCTION', name='environment'), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=True),
    sa.Column('release_id', sa.String(length=64), nullable=True),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('state', sa.Enum('DRAFT', 'VALIDATING', 'VALIDATED', 'VALIDATION_FAILED', 'AWAITING_APPROVAL', 'APPROVED', 'DEPLOYING', 'DEPLOYED', 'DEPLOY_FAILED', 'ROLLED_BACK', name='changesetstate'), nullable=False),
    sa.Column('source_files', sa.JSON(), nullable=True),
    sa.Column('package_manifest', sa.JSON(), nullable=True),
    sa.Column('diff', sa.JSON(), nullable=True),
    sa.Column('test_level', sa.String(length=32), nullable=False),
    sa.Column('run_tests', sa.JSON(), nullable=True),
    sa.Column('validation_deploy_id', sa.String(length=64), nullable=True),
    sa.Column('validation_result', sa.JSON(), nullable=True),
    sa.Column('deploy_id', sa.String(length=64), nullable=True),
    sa.Column('deploy_result', sa.JSON(), nullable=True),
    sa.Column('approval_id', sa.String(length=64), nullable=True),
    sa.Column('rollback_plan', sa.JSON(), nullable=True),
    sa.Column('rolled_back_deploy_id', sa.String(length=64), nullable=True),
    sa.Column('verified', sa.Boolean(), nullable=False),
    sa.Column('verification', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('change_sets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_change_sets_agent_run_id'), ['agent_run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_change_sets_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_change_sets_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_change_sets_release_id'), ['release_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_change_sets_salesforce_connection_id'), ['salesforce_connection_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_change_sets_user_id'), ['user_id'], unique=False)
        batch_op.create_index('ix_cs_project_created', ['project_id', 'created_at'], unique=False)

    op.create_table('companies',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('slug', sa.String(length=120), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('plan', sa.Enum('TRIAL', 'STARTER', 'BUSINESS', 'ENTERPRISE', name='plantier'), nullable=False),
    sa.Column('sso_domains', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('companies', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_companies_slug'), ['slug'], unique=True)

    op.create_table('data_jobs',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('salesforce_connection_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=True),
    sa.Column('approval_id', sa.String(length=64), nullable=True),
    sa.Column('operation', sa.String(length=32), nullable=False),
    sa.Column('sobject', sa.String(length=128), nullable=False),
    sa.Column('sf_job_id', sa.String(length=64), nullable=True),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('records_total', sa.Integer(), nullable=False),
    sa.Column('records_processed', sa.Integer(), nullable=False),
    sa.Column('records_failed', sa.Integer(), nullable=False),
    sa.Column('plan', sa.JSON(), nullable=True),
    sa.Column('failures_sample', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('data_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_data_jobs_agent_run_id'), ['agent_run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_data_jobs_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_data_jobs_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_data_jobs_salesforce_connection_id'), ['salesforce_connection_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_data_jobs_sf_job_id'), ['sf_job_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_data_jobs_user_id'), ['user_id'], unique=False)
        batch_op.create_index('ix_datajob_project', ['project_id', 'created_at'], unique=False)

    op.create_table('deployments',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=True),
    sa.Column('change_set_id', sa.String(length=64), nullable=True),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('salesforce_connection_id', sa.String(length=64), nullable=False),
    sa.Column('environment', sa.Enum('DEVELOPMENT', 'SANDBOX', 'UAT', 'PRODUCTION', name='environment'), nullable=False),
    sa.Column('salesforce_deploy_id', sa.String(length=64), nullable=True),
    sa.Column('check_only', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=64), nullable=False),
    sa.Column('components_total', sa.Integer(), nullable=False),
    sa.Column('components_failed', sa.Integer(), nullable=False),
    sa.Column('tests_total', sa.Integer(), nullable=False),
    sa.Column('tests_failed', sa.Integer(), nullable=False),
    sa.Column('package_manifest', sa.JSON(), nullable=True),
    sa.Column('errors', sa.JSON(), nullable=True),
    sa.Column('duration_ms', sa.Float(), nullable=False),
    sa.Column('verified', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('deployments', schema=None) as batch_op:
        batch_op.create_index('ix_deploy_project', ['project_id', 'created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_deployments_agent_run_id'), ['agent_run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_deployments_change_set_id'), ['change_set_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_deployments_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_deployments_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_deployments_salesforce_connection_id'), ['salesforce_connection_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_deployments_salesforce_deploy_id'), ['salesforce_deploy_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_deployments_user_id'), ['user_id'], unique=False)

    op.create_table('invitations',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('role', sa.Enum('PROJECT_ADMIN', 'SALESFORCE_ADMIN', 'RELEASE_MANAGER', 'SECURITY_ADMIN', 'DEVELOPER', 'USER', 'AUDITOR', 'VIEWER', name='projectrole'), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('invited_by', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('invitations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_invitations_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_invitations_email'), ['email'], unique=False)
        batch_op.create_index(batch_op.f('ix_invitations_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_invitations_token_hash'), ['token_hash'], unique=True)
        batch_op.create_index('ix_invite_project', ['project_id', 'email'], unique=False)

    op.create_table('llm_usage',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=True),
    sa.Column('credential_id', sa.String(length=64), nullable=True),
    sa.Column('provider', sa.String(length=60), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('tier', sa.String(length=20), nullable=False),
    sa.Column('requests', sa.Integer(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('estimated_cost_usd', sa.Float(), nullable=False),
    sa.Column('byok', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('llm_usage', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_llm_usage_agent_run_id'), ['agent_run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_llm_usage_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_llm_usage_project_id'), ['project_id'], unique=False)
        batch_op.create_index('ix_llmusage_project_created', ['project_id', 'created_at'], unique=False)

    op.create_table('mcp_servers',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('transport', sa.String(length=16), nullable=False),
    sa.Column('command', sa.String(length=512), nullable=True),
    sa.Column('args', sa.JSON(), nullable=True),
    sa.Column('url', sa.String(length=512), nullable=True),
    sa.Column('secret_ref', sa.Text(), nullable=True),
    sa.Column('env', sa.JSON(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('tool_allowlist', sa.JSON(), nullable=True),
    sa.Column('risk_overrides', sa.JSON(), nullable=True),
    sa.Column('discovered_tools', sa.JSON(), nullable=True),
    sa.Column('last_discovery_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'name', name='uq_mcp_name')
    )
    with op.batch_alter_table('mcp_servers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_mcp_servers_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_mcp_servers_project_id'), ['project_id'], unique=False)

    op.create_table('oauth_states',
    sa.Column('state', sa.String(length=128), nullable=False),
    sa.Column('provider', sa.String(length=40), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('code_verifier', sa.String(length=256), nullable=False),
    sa.Column('login_url', sa.String(length=512), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.Column('redirect_after', sa.String(length=512), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('state')
    )
    with op.batch_alter_table('oauth_states', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_oauth_states_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_oauth_states_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_oauth_states_user_id'), ['user_id'], unique=False)

    op.create_table('org_knowledge',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('salesforce_connection_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.Enum('OBJECT', 'FIELD', 'AUTOMATION', 'APEX', 'REPORT', 'PERMISSION', 'DEPLOYMENT', 'FAILURE', 'PATTERN', 'DIAGNOSIS', name='knowledgekind'), nullable=False),
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=True),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('hit_count', sa.Integer(), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('salesforce_connection_id', 'kind', 'key', name='uq_knowledge')
    )
    with op.batch_alter_table('org_knowledge', schema=None) as batch_op:
        batch_op.create_index('ix_knowledge_lookup', ['salesforce_connection_id', 'kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_org_knowledge_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_org_knowledge_key'), ['key'], unique=False)
        batch_op.create_index(batch_op.f('ix_org_knowledge_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_org_knowledge_salesforce_connection_id'), ['salesforce_connection_id'], unique=False)

    op.create_table('releases',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('source_environment', sa.Enum('DEVELOPMENT', 'SANDBOX', 'UAT', 'PRODUCTION', name='environment'), nullable=True),
    sa.Column('target_environment', sa.Enum('DEVELOPMENT', 'SANDBOX', 'UAT', 'PRODUCTION', name='environment'), nullable=False),
    sa.Column('state', sa.Enum('DRAFT', 'VALIDATING', 'AWAITING_APPROVAL', 'APPROVED', 'DEPLOYING', 'RELEASED', 'FAILED', 'ROLLED_BACK', name='releasestate'), nullable=False),
    sa.Column('change_set_ids', sa.JSON(), nullable=True),
    sa.Column('approval_id', sa.String(length=64), nullable=True),
    sa.Column('verification', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('releases', schema=None) as batch_op:
        batch_op.create_index('ix_release_project', ['project_id', 'created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_releases_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_releases_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_releases_user_id'), ['user_id'], unique=False)

    op.create_table('repositories',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('integration_id', sa.String(length=64), nullable=False),
    sa.Column('provider', sa.Enum('JIRA', 'GITHUB', 'BITBUCKET', 'SLACK', 'TEAMS', name='integrationkind'), nullable=False),
    sa.Column('full_name', sa.String(length=300), nullable=False),
    sa.Column('default_branch', sa.String(length=200), nullable=False),
    sa.Column('allowed_branch_patterns', sa.JSON(), nullable=True),
    sa.Column('allowed_paths', sa.JSON(), nullable=True),
    sa.Column('require_pull_request', sa.Boolean(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'provider', 'full_name', name='uq_repository')
    )
    with op.batch_alter_table('repositories', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_repositories_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_repositories_integration_id'), ['integration_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_repositories_project_id'), ['project_id'], unique=False)

    op.create_table('run_events',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('event_type', sa.String(length=60), nullable=False),
    sa.Column('data', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agent_run_id', 'sequence', name='uq_run_event_seq')
    )
    with op.batch_alter_table('run_events', schema=None) as batch_op:
        batch_op.create_index('ix_run_event_stream', ['agent_run_id', 'sequence'], unique=False)
        batch_op.create_index(batch_op.f('ix_run_events_agent_run_id'), ['agent_run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_run_events_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_run_events_project_id'), ['project_id'], unique=False)

    op.create_table('tool_executions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=False),
    sa.Column('conversation_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('salesforce_connection_id', sa.String(length=64), nullable=True),
    sa.Column('tool_name', sa.String(length=128), nullable=False),
    sa.Column('tool_provider', sa.String(length=60), nullable=False),
    sa.Column('tool_use_id', sa.String(length=128), nullable=False),
    sa.Column('arguments', sa.JSON(), nullable=True),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.Column('risk_level', sa.Enum('LOW', 'MEDIUM', 'HIGH', 'CRITICAL', name='risklevel'), nullable=False),
    sa.Column('approval_state', sa.Enum('NOT_REQUIRED', 'PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', name='approvalstate'), nullable=False),
    sa.Column('execution_state', sa.Enum('PLANNED', 'PROPOSED', 'APPROVED', 'EXECUTING', 'SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'FAILED', 'SKIPPED', name='executionstate'), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('salesforce_object', sa.String(length=128), nullable=True),
    sa.Column('record_ids', sa.JSON(), nullable=True),
    sa.Column('error_type', sa.String(length=128), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('duration_ms', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('tool_executions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tool_executions_agent_run_id'), ['agent_run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tool_executions_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tool_executions_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tool_executions_idempotency_key'), ['idempotency_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_tool_executions_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tool_executions_tool_name'), ['tool_name'], unique=False)
        batch_op.create_index(batch_op.f('ix_tool_executions_user_id'), ['user_id'], unique=False)
        batch_op.create_index('ix_tool_project', ['project_id', 'created_at'], unique=False)
        batch_op.create_index('ix_tool_run', ['agent_run_id', 'created_at'], unique=False)

    op.create_table('users',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('idp_kind', sa.Enum('LOCAL', 'OIDC', 'SAML', name='identityproviderkind'), nullable=False),
    sa.Column('idp_subject', sa.String(length=320), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_users_email'), ['email'], unique=True)
        batch_op.create_index(batch_op.f('ix_users_idp_subject'), ['idp_subject'], unique=False)

    op.create_table('approval_decisions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('approval_id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('role', sa.Enum('PROJECT_ADMIN', 'SALESFORCE_ADMIN', 'RELEASE_MANAGER', 'SECURITY_ADMIN', 'DEVELOPER', 'USER', 'AUDITOR', 'VIEWER', name='projectrole'), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('decided_change_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['approval_id'], ['approvals.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('approval_id', 'user_id', name='uq_approval_voter')
    )
    with op.batch_alter_table('approval_decisions', schema=None) as batch_op:
        batch_op.create_index('ix_appr_dec_approval', ['approval_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_approval_decisions_approval_id'), ['approval_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_approval_decisions_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_approval_decisions_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_approval_decisions_user_id'), ['user_id'], unique=False)

    op.create_table('company_memberships',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('role', sa.Enum('PLATFORM_OWNER', 'COMPANY_ADMIN', 'COMPANY_MEMBER', 'COMPANY_AUDITOR', name='companyrole'), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'user_id', name='uq_company_membership')
    )
    with op.batch_alter_table('company_memberships', schema=None) as batch_op:
        batch_op.create_index('ix_company_membership_user', ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_company_memberships_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_company_memberships_external_id'), ['external_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_company_memberships_user_id'), ['user_id'], unique=False)

    op.create_table('projects',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('slug', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('promotion_path', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'slug', name='uq_project_slug')
    )
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.create_index('ix_project_company', ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_projects_company_id'), ['company_id'], unique=False)

    op.create_table('sso_configurations',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.Enum('LOCAL', 'OIDC', 'SAML', name='identityproviderkind'), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('issuer', sa.String(length=512), nullable=True),
    sa.Column('client_id', sa.String(length=512), nullable=True),
    sa.Column('client_secret_ref', sa.Text(), nullable=True),
    sa.Column('jwks_uri', sa.String(length=512), nullable=True),
    sa.Column('redirect_uri', sa.String(length=512), nullable=True),
    sa.Column('metadata_url', sa.String(length=512), nullable=True),
    sa.Column('scopes', sa.String(length=300), nullable=False),
    sa.Column('group_mappings', sa.JSON(), nullable=True),
    sa.Column('default_project_id', sa.String(length=64), nullable=True),
    sa.Column('default_project_role', sa.Enum('PROJECT_ADMIN', 'SALESFORCE_ADMIN', 'RELEASE_MANAGER', 'SECURITY_ADMIN', 'DEVELOPER', 'USER', 'AUDITOR', 'VIEWER', name='projectrole'), nullable=False),
    sa.Column('scim_enabled', sa.Boolean(), nullable=False),
    sa.Column('scim_token_ref', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'kind', name='uq_sso_kind')
    )
    with op.batch_alter_table('sso_configurations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sso_configurations_company_id'), ['company_id'], unique=False)

    op.create_table('subscriptions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('plan', sa.Enum('TRIAL', 'STARTER', 'BUSINESS', 'ENTERPRISE', name='plantier'), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('max_projects', sa.Integer(), nullable=False),
    sa.Column('max_users', sa.Integer(), nullable=False),
    sa.Column('max_salesforce_connections', sa.Integer(), nullable=False),
    sa.Column('monthly_run_allowance', sa.Integer(), nullable=False),
    sa.Column('features', sa.JSON(), nullable=True),
    sa.Column('external_customer_ref', sa.String(length=200), nullable=True),
    sa.Column('current_period_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_subscriptions_company_id'), ['company_id'], unique=True)

    op.create_table('conversations',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('salesforce_connection_id', sa.String(length=64), nullable=True),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.create_index('ix_conv_project_created', ['project_id', 'created_at'], unique=False)
        batch_op.create_index('ix_conv_user_created', ['user_id', 'created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversations_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversations_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversations_salesforce_connection_id'), ['salesforce_connection_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversations_user_id'), ['user_id'], unique=False)

    op.create_table('integration_connections',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.Enum('JIRA', 'GITHUB', 'BITBUCKET', 'SLACK', 'TEAMS', name='integrationkind'), nullable=False),
    sa.Column('account', sa.String(length=200), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('base_url', sa.String(length=512), nullable=True),
    sa.Column('access_token_ref', sa.Text(), nullable=True),
    sa.Column('refresh_token_ref', sa.Text(), nullable=True),
    sa.Column('token_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('scopes', sa.String(length=512), nullable=False),
    sa.Column('config', sa.JSON(), nullable=True),
    sa.Column('connected_by', sa.String(length=64), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('last_validated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'kind', 'account', name='uq_integration')
    )
    with op.batch_alter_table('integration_connections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_integration_connections_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_integration_connections_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_integration_connections_project_id'), ['project_id'], unique=False)
        batch_op.create_index('ix_integration_project', ['project_id'], unique=False)

    op.create_table('llm_credentials',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('provider', sa.Enum('ANTHROPIC', 'OPENAI', 'AZURE_OPENAI', 'BEDROCK', 'GOOGLE', 'VERTEX', 'MISTRAL', 'GROQ', 'DEEPSEEK', 'TOGETHER', 'OLLAMA', 'OPENAI_COMPATIBLE', name='llmproviderkind'), nullable=False),
    sa.Column('secret_ref', sa.Text(), nullable=True),
    sa.Column('base_url', sa.String(length=512), nullable=True),
    sa.Column('region', sa.String(length=64), nullable=True),
    sa.Column('config', sa.JSON(), nullable=True),
    sa.Column('tier_models', sa.JSON(), nullable=True),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('last_tested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_test_ok', sa.Boolean(), nullable=True),
    sa.Column('last_test_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'name', name='uq_llm_credential_name')
    )
    with op.batch_alter_table('llm_credentials', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_llm_credentials_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_llm_credentials_project_id'), ['project_id'], unique=False)
        batch_op.create_index('ix_llmcred_project', ['project_id'], unique=False)

    op.create_table('project_memberships',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('role', sa.Enum('PROJECT_ADMIN', 'SALESFORCE_ADMIN', 'RELEASE_MANAGER', 'SECURITY_ADMIN', 'DEVELOPER', 'USER', 'AUDITOR', 'VIEWER', name='projectrole'), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('invited_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'user_id', name='uq_project_membership')
    )
    with op.batch_alter_table('project_memberships', schema=None) as batch_op:
        batch_op.create_index('ix_project_membership_user', ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_project_memberships_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_project_memberships_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_project_memberships_user_id'), ['user_id'], unique=False)

    op.create_table('project_policies',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('allow_production_mutations', sa.Boolean(), nullable=False),
    sa.Column('approval_ttl_seconds', sa.Integer(), nullable=False),
    sa.Column('max_agent_steps', sa.Integer(), nullable=False),
    sa.Column('max_execution_seconds', sa.Integer(), nullable=False),
    sa.Column('max_tool_calls', sa.Integer(), nullable=False),
    sa.Column('require_separate_approver', sa.Boolean(), nullable=False),
    sa.Column('approver_matrix', sa.JSON(), nullable=True),
    sa.Column('disabled_tools', sa.JSON(), nullable=True),
    sa.Column('max_bulk_records', sa.Integer(), nullable=False),
    sa.Column('allowed_environments', sa.JSON(), nullable=True),
    sa.Column('allowed_llm_providers', sa.JSON(), nullable=True),
    sa.Column('allow_llm_fallback', sa.Boolean(), nullable=False),
    sa.Column('retain_conversation_days', sa.Integer(), nullable=False),
    sa.Column('retain_tool_payload_days', sa.Integer(), nullable=False),
    sa.Column('retain_document_days', sa.Integer(), nullable=False),
    sa.Column('audit_retention_days', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('project_policies', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_project_policies_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_project_policies_project_id'), ['project_id'], unique=True)

    op.create_table('salesforce_connections',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('connected_by', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=120), nullable=False),
    sa.Column('environment', sa.Enum('DEVELOPMENT', 'SANDBOX', 'UAT', 'PRODUCTION', name='environment'), nullable=False),
    sa.Column('sf_org_id', sa.String(length=32), nullable=False),
    sa.Column('sf_user_id', sa.String(length=32), nullable=False),
    sa.Column('username', sa.String(length=320), nullable=False),
    sa.Column('instance_url', sa.String(length=512), nullable=False),
    sa.Column('login_url', sa.String(length=512), nullable=False),
    sa.Column('is_sandbox', sa.Boolean(), nullable=False),
    sa.Column('org_type', sa.String(length=64), nullable=False),
    sa.Column('api_version', sa.String(length=16), nullable=False),
    sa.Column('access_token_ref', sa.Text(), nullable=False),
    sa.Column('refresh_token_ref', sa.Text(), nullable=True),
    sa.Column('token_issued_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('token_fingerprint', sa.String(length=32), nullable=False),
    sa.Column('scopes', sa.String(length=512), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('last_validated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'sf_org_id', name='uq_project_sf_org')
    )
    with op.batch_alter_table('salesforce_connections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_salesforce_connections_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_salesforce_connections_connected_by'), ['connected_by'], unique=False)
        batch_op.create_index(batch_op.f('ix_salesforce_connections_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_salesforce_connections_sf_org_id'), ['sf_org_id'], unique=False)
        batch_op.create_index('ix_sfconn_company', ['company_id'], unique=False)
        batch_op.create_index('ix_sfconn_project', ['project_id'], unique=False)

    op.create_table('agent_runs',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('conversation_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('salesforce_connection_id', sa.String(length=64), nullable=True),
    sa.Column('correlation_id', sa.String(length=64), nullable=False),
    sa.Column('state', sa.Enum('CREATED', 'QUEUED', 'PLANNING', 'INSPECTING', 'WAITING_FOR_APPROVAL', 'EXECUTING', 'VERIFYING', 'COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED', name='runstate'), nullable=False),
    sa.Column('user_request', sa.Text(), nullable=False),
    sa.Column('steps_used', sa.Integer(), nullable=False),
    sa.Column('max_steps', sa.Integer(), nullable=False),
    sa.Column('tool_calls_used', sa.Integer(), nullable=False),
    sa.Column('llm_provider', sa.String(length=60), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('model_tier', sa.String(length=20), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('estimated_cost_usd', sa.Float(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('error_code', sa.String(length=80), nullable=True),
    sa.Column('final_text', sa.Text(), nullable=False),
    sa.Column('duration_ms', sa.Float(), nullable=False),
    sa.Column('transcript', sa.JSON(), nullable=True),
    sa.Column('pending_tool_results', sa.JSON(), nullable=True),
    sa.Column('pending_approval_ids', sa.JSON(), nullable=True),
    sa.Column('claimed_by', sa.String(length=80), nullable=True),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_requested', sa.Boolean(), nullable=False),
    sa.Column('cancel_requested_by', sa.String(length=64), nullable=True),
    sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('agent_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agent_runs_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_runs_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_runs_correlation_id'), ['correlation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_runs_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_runs_user_id'), ['user_id'], unique=False)
        batch_op.create_index('ix_run_conv', ['conversation_id', 'created_at'], unique=False)
        batch_op.create_index('ix_run_project_created', ['project_id', 'created_at'], unique=False)
        batch_op.create_index('ix_run_state', ['state'], unique=False)

    op.create_table('messages',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('conversation_id', sa.String(length=64), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('blocks', sa.JSON(), nullable=True),
    sa.Column('agent_run_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_messages_agent_run_id'), ['agent_run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_messages_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index('ix_msg_conv_created', ['conversation_id', 'created_at'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.drop_index('ix_msg_conv_created')
        batch_op.drop_index(batch_op.f('ix_messages_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_messages_agent_run_id'))

    op.drop_table('messages')
    with op.batch_alter_table('agent_runs', schema=None) as batch_op:
        batch_op.drop_index('ix_run_state')
        batch_op.drop_index('ix_run_project_created')
        batch_op.drop_index('ix_run_conv')
        batch_op.drop_index(batch_op.f('ix_agent_runs_user_id'))
        batch_op.drop_index(batch_op.f('ix_agent_runs_project_id'))
        batch_op.drop_index(batch_op.f('ix_agent_runs_correlation_id'))
        batch_op.drop_index(batch_op.f('ix_agent_runs_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_agent_runs_company_id'))

    op.drop_table('agent_runs')
    with op.batch_alter_table('salesforce_connections', schema=None) as batch_op:
        batch_op.drop_index('ix_sfconn_project')
        batch_op.drop_index('ix_sfconn_company')
        batch_op.drop_index(batch_op.f('ix_salesforce_connections_sf_org_id'))
        batch_op.drop_index(batch_op.f('ix_salesforce_connections_project_id'))
        batch_op.drop_index(batch_op.f('ix_salesforce_connections_connected_by'))
        batch_op.drop_index(batch_op.f('ix_salesforce_connections_company_id'))

    op.drop_table('salesforce_connections')
    with op.batch_alter_table('project_policies', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_project_policies_project_id'))
        batch_op.drop_index(batch_op.f('ix_project_policies_company_id'))

    op.drop_table('project_policies')
    with op.batch_alter_table('project_memberships', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_project_memberships_user_id'))
        batch_op.drop_index(batch_op.f('ix_project_memberships_project_id'))
        batch_op.drop_index(batch_op.f('ix_project_memberships_company_id'))
        batch_op.drop_index('ix_project_membership_user')

    op.drop_table('project_memberships')
    with op.batch_alter_table('llm_credentials', schema=None) as batch_op:
        batch_op.drop_index('ix_llmcred_project')
        batch_op.drop_index(batch_op.f('ix_llm_credentials_project_id'))
        batch_op.drop_index(batch_op.f('ix_llm_credentials_company_id'))

    op.drop_table('llm_credentials')
    with op.batch_alter_table('integration_connections', schema=None) as batch_op:
        batch_op.drop_index('ix_integration_project')
        batch_op.drop_index(batch_op.f('ix_integration_connections_project_id'))
        batch_op.drop_index(batch_op.f('ix_integration_connections_kind'))
        batch_op.drop_index(batch_op.f('ix_integration_connections_company_id'))

    op.drop_table('integration_connections')
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_conversations_user_id'))
        batch_op.drop_index(batch_op.f('ix_conversations_salesforce_connection_id'))
        batch_op.drop_index(batch_op.f('ix_conversations_project_id'))
        batch_op.drop_index(batch_op.f('ix_conversations_company_id'))
        batch_op.drop_index('ix_conv_user_created')
        batch_op.drop_index('ix_conv_project_created')

    op.drop_table('conversations')
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_subscriptions_company_id'))

    op.drop_table('subscriptions')
    with op.batch_alter_table('sso_configurations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sso_configurations_company_id'))

    op.drop_table('sso_configurations')
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_projects_company_id'))
        batch_op.drop_index('ix_project_company')

    op.drop_table('projects')
    with op.batch_alter_table('company_memberships', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_company_memberships_user_id'))
        batch_op.drop_index(batch_op.f('ix_company_memberships_external_id'))
        batch_op.drop_index(batch_op.f('ix_company_memberships_company_id'))
        batch_op.drop_index('ix_company_membership_user')

    op.drop_table('company_memberships')
    with op.batch_alter_table('approval_decisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_approval_decisions_user_id'))
        batch_op.drop_index(batch_op.f('ix_approval_decisions_project_id'))
        batch_op.drop_index(batch_op.f('ix_approval_decisions_company_id'))
        batch_op.drop_index(batch_op.f('ix_approval_decisions_approval_id'))
        batch_op.drop_index('ix_appr_dec_approval')

    op.drop_table('approval_decisions')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_idp_subject'))
        batch_op.drop_index(batch_op.f('ix_users_email'))

    op.drop_table('users')
    with op.batch_alter_table('tool_executions', schema=None) as batch_op:
        batch_op.drop_index('ix_tool_run')
        batch_op.drop_index('ix_tool_project')
        batch_op.drop_index(batch_op.f('ix_tool_executions_user_id'))
        batch_op.drop_index(batch_op.f('ix_tool_executions_tool_name'))
        batch_op.drop_index(batch_op.f('ix_tool_executions_project_id'))
        batch_op.drop_index(batch_op.f('ix_tool_executions_idempotency_key'))
        batch_op.drop_index(batch_op.f('ix_tool_executions_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_tool_executions_company_id'))
        batch_op.drop_index(batch_op.f('ix_tool_executions_agent_run_id'))

    op.drop_table('tool_executions')
    with op.batch_alter_table('run_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_run_events_project_id'))
        batch_op.drop_index(batch_op.f('ix_run_events_company_id'))
        batch_op.drop_index(batch_op.f('ix_run_events_agent_run_id'))
        batch_op.drop_index('ix_run_event_stream')

    op.drop_table('run_events')
    with op.batch_alter_table('repositories', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_repositories_project_id'))
        batch_op.drop_index(batch_op.f('ix_repositories_integration_id'))
        batch_op.drop_index(batch_op.f('ix_repositories_company_id'))

    op.drop_table('repositories')
    with op.batch_alter_table('releases', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_releases_user_id'))
        batch_op.drop_index(batch_op.f('ix_releases_project_id'))
        batch_op.drop_index(batch_op.f('ix_releases_company_id'))
        batch_op.drop_index('ix_release_project')

    op.drop_table('releases')
    with op.batch_alter_table('org_knowledge', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_org_knowledge_salesforce_connection_id'))
        batch_op.drop_index(batch_op.f('ix_org_knowledge_project_id'))
        batch_op.drop_index(batch_op.f('ix_org_knowledge_key'))
        batch_op.drop_index(batch_op.f('ix_org_knowledge_company_id'))
        batch_op.drop_index('ix_knowledge_lookup')

    op.drop_table('org_knowledge')
    with op.batch_alter_table('oauth_states', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_oauth_states_user_id'))
        batch_op.drop_index(batch_op.f('ix_oauth_states_project_id'))
        batch_op.drop_index(batch_op.f('ix_oauth_states_company_id'))

    op.drop_table('oauth_states')
    with op.batch_alter_table('mcp_servers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_mcp_servers_project_id'))
        batch_op.drop_index(batch_op.f('ix_mcp_servers_company_id'))

    op.drop_table('mcp_servers')
    with op.batch_alter_table('llm_usage', schema=None) as batch_op:
        batch_op.drop_index('ix_llmusage_project_created')
        batch_op.drop_index(batch_op.f('ix_llm_usage_project_id'))
        batch_op.drop_index(batch_op.f('ix_llm_usage_company_id'))
        batch_op.drop_index(batch_op.f('ix_llm_usage_agent_run_id'))

    op.drop_table('llm_usage')
    with op.batch_alter_table('invitations', schema=None) as batch_op:
        batch_op.drop_index('ix_invite_project')
        batch_op.drop_index(batch_op.f('ix_invitations_token_hash'))
        batch_op.drop_index(batch_op.f('ix_invitations_project_id'))
        batch_op.drop_index(batch_op.f('ix_invitations_email'))
        batch_op.drop_index(batch_op.f('ix_invitations_company_id'))

    op.drop_table('invitations')
    with op.batch_alter_table('deployments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_deployments_user_id'))
        batch_op.drop_index(batch_op.f('ix_deployments_salesforce_deploy_id'))
        batch_op.drop_index(batch_op.f('ix_deployments_salesforce_connection_id'))
        batch_op.drop_index(batch_op.f('ix_deployments_project_id'))
        batch_op.drop_index(batch_op.f('ix_deployments_company_id'))
        batch_op.drop_index(batch_op.f('ix_deployments_change_set_id'))
        batch_op.drop_index(batch_op.f('ix_deployments_agent_run_id'))
        batch_op.drop_index('ix_deploy_project')

    op.drop_table('deployments')
    with op.batch_alter_table('data_jobs', schema=None) as batch_op:
        batch_op.drop_index('ix_datajob_project')
        batch_op.drop_index(batch_op.f('ix_data_jobs_user_id'))
        batch_op.drop_index(batch_op.f('ix_data_jobs_sf_job_id'))
        batch_op.drop_index(batch_op.f('ix_data_jobs_salesforce_connection_id'))
        batch_op.drop_index(batch_op.f('ix_data_jobs_project_id'))
        batch_op.drop_index(batch_op.f('ix_data_jobs_company_id'))
        batch_op.drop_index(batch_op.f('ix_data_jobs_agent_run_id'))

    op.drop_table('data_jobs')
    with op.batch_alter_table('companies', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_companies_slug'))

    op.drop_table('companies')
    with op.batch_alter_table('change_sets', schema=None) as batch_op:
        batch_op.drop_index('ix_cs_project_created')
        batch_op.drop_index(batch_op.f('ix_change_sets_user_id'))
        batch_op.drop_index(batch_op.f('ix_change_sets_salesforce_connection_id'))
        batch_op.drop_index(batch_op.f('ix_change_sets_release_id'))
        batch_op.drop_index(batch_op.f('ix_change_sets_project_id'))
        batch_op.drop_index(batch_op.f('ix_change_sets_company_id'))
        batch_op.drop_index(batch_op.f('ix_change_sets_agent_run_id'))

    op.drop_table('change_sets')
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.drop_index('ix_audit_project_created')
        batch_op.drop_index(batch_op.f('ix_audit_events_user_id'))
        batch_op.drop_index(batch_op.f('ix_audit_events_project_id'))
        batch_op.drop_index(batch_op.f('ix_audit_events_correlation_id'))
        batch_op.drop_index(batch_op.f('ix_audit_events_company_id'))
        batch_op.drop_index(batch_op.f('ix_audit_events_agent_run_id'))
        batch_op.drop_index(batch_op.f('ix_audit_events_action'))
        batch_op.drop_index('ix_audit_company_created')
        batch_op.drop_index('ix_audit_action')

    op.drop_table('audit_events')
    with op.batch_alter_table('approvals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_approvals_user_id'))
        batch_op.drop_index(batch_op.f('ix_approvals_project_id'))
        batch_op.drop_index(batch_op.f('ix_approvals_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_approvals_company_id'))
        batch_op.drop_index(batch_op.f('ix_approvals_change_hash'))
        batch_op.drop_index(batch_op.f('ix_approvals_agent_run_id'))
        batch_op.drop_index('ix_appr_user_state')
        batch_op.drop_index('ix_appr_project_state')

    op.drop_table('approvals')

    _drop_enum_types()


#: Every named ENUM this revision creates. Postgres treats a type as an object
#: in its own right, so dropping the tables that use one leaves the type behind.
#: A `downgrade base` that skips them looks successful and leaves the database
#: in a state where the next `upgrade head` dies on
#: `type "environment" already exists` — which is exactly the failure a
#: downgrade is supposed to let you recover from.
#:
#: SQLite has no such objects, which is why the round-trip appeared to work
#: until it was run against the database this actually ships on.
ENUM_TYPES = (
    'approvalstate',
    'changesetstate',
    'companyrole',
    'environment',
    'executionstate',
    'identityproviderkind',
    'integrationkind',
    'knowledgekind',
    'llmproviderkind',
    'plantier',
    'projectrole',
    'releasestate',
    'risklevel',
    'runstate',
)


def _drop_enum_types() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    for name in ENUM_TYPES:
        sa.Enum(name=name).drop(bind, checkfirst=True)
