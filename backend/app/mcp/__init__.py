"""MCP as an additional tool provider.

MCP is a transport, not a trust boundary. A tool discovered from an MCP server
enters the same pipeline as a native Salesforce tool — deterministic risk
classification, approval gating, audit, verification and untrusted-result
handling — and it can never bypass any of them. What MCP buys us is reach:
capabilities we have not implemented natively become available without
touching the agent runtime.
"""
