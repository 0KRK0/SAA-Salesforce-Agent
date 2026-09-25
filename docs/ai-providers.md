# AI providers

The platform is model-independent in fact, not in marketing. The agent runtime
speaks one message shape; every provider translates to and from it. Swapping
Anthropic for Azure OpenAI changes which class is instantiated and nothing
else — no prompt changes, no tool changes, no runtime changes.

## What is actually implemented

| Provider | Status | Auth | Notes |
| --- | --- | --- | --- |
| Anthropic | **Implemented** | API key | Messages API. The canonical shape — no translation. |
| OpenAI | **Implemented** | API key | Chat Completions. |
| Azure OpenAI | **Implemented** | `api-key` header | Addresses a *deployment*, not a model. |
| Google Gemini | **Implemented** | API key | Generative Language API. |
| Mistral | **Implemented** | API key | OpenAI-compatible. |
| Groq | **Implemented** | API key | OpenAI-compatible. |
| DeepSeek | **Implemented** | API key | OpenAI-compatible. |
| Together AI | **Implemented** | API key | OpenAI-compatible. |
| Ollama | **Implemented** | none | Self-hosted. Tool support depends on the model pulled. |
| OpenAI-compatible | **Implemented** | API key | Any gateway speaking the OpenAI API. |
| AWS Bedrock | **Not implemented** | — | Needs SigV4 signing and the AWS credential chain. |
| Google Vertex AI | **Not implemented** | — | Needs service-account assertions, not an API key. |

The two unimplemented providers **raise at construction**. A deployment that
selects one fails at startup with the reason, rather than failing at a
customer's first request. They are listed in the UI as unavailable with the
explanation, because "we don't support that, here's why" is a better answer
than an empty list.

No vendor SDKs are used. Every provider is plain HTTP over `httpx`, which gives
one retry policy, one error taxonomy, one place where timeouts and redaction
live, and one testing story (`httpx.MockTransport`) for all of them.

## Whose key runs the work

Resolution order, and it is the commercial contract of the product:

1. **The project's own credential (BYOK).** Stored as a secret-store reference
   bound to that company and project. Requests run on the customer's vendor
   account, under their own terms.
2. **The deployment's key** — *only* when `FEATURE_PLATFORM_MANAGED_AI=true`.
   Off by default, because silently spending the operator's key on a customer's
   work is a billing surprise, and silently routing a customer's data through
   the operator's vendor account is a compliance one.

Two project-level guard rails sit above both:

- `allowed_llm_providers` — if a project says its data may only go to Azure
  OpenAI, nothing else is reachable, not even as a fallback. A credential for a
  forbidden provider is refused at the point of configuration rather than
  stored and never used.
- `allow_llm_fallback` — off by default. A fallback moves customer data to a
  different vendor, so it happens only when someone chose it.

**A credential rejection is never failed over.** Sending the same customer data
to a second vendor because the first said "unauthorized" is a data-residency
decision, not a retry. Only retryable failures — capacity, timeouts, 5xx — move
to the next route.

## Tiers

Each credential maps three tiers to concrete models:

- `FAST` — cheap and quick: classification, short summaries.
- `BALANCED` — the default for agent runs.
- `ADVANCED` — the hardest reasoning.

A tier with no model chosen falls back to the deployment's configured model for
that tier, then to the provider's catalog default. Any model name the vendor
offers can be typed; the catalog defaults are **not** a supported-model list,
because vendors ship models faster than any hardcoded list can track.

## Secrets

A provider key is **write-only through the API**. It is posted once, handed
straight to the secret store, and from then on only a fingerprint and the last
four characters are ever returned. There is no reveal endpoint, because there
is no code path that resolves a stored key back to a caller.

Rotation replaces the reference and clears the stored test result — a rotated
key that has not been tested is shown as "Not tested", not "Connected".

## Test connection is a real call

The Test button makes a live request to the provider. A test that only checked
the shape of a key would report "Connected" for a revoked one, which is exactly
the class of lie this product refuses to tell. The UI shows three distinct
states and never conflates them:

- **Connected** — a live call succeeded.
- **Failed** — a live call was made and the provider rejected it.
- **Not tested** — a key is stored and nobody has checked it yet.

## Cost estimates

Token counts are exact — providers report them. Costs are **estimates from
published list prices captured on a stated date**, and the API returns that
date alongside every figure.

A model with no price on file reports `priced: false` and a zero cost rather
than a guessed number. Someone will budget against a spend dashboard; a blank
prompts them to enter their real rate, an invented figure does not. Negotiated
rates and committed-use discounts are not reflected. These are not invoices.

Self-hosted inference (Ollama) is priced at zero and says so: zero is the true
marginal API cost, and the infrastructure cost is real but is not a per-token
number this system can know.

## What is recorded

`llm_usage` holds provider, model, tier, token counts, an estimated cost and
whether it was BYOK. There is no column that can hold a prompt, a completion or
a tool argument, and none of them are written anywhere else either.

## Configuration

See `.env.example`. The deployment-managed keys (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, …) are fallbacks that only take effect with
`FEATURE_PLATFORM_MANAGED_AI=true`. `CLAUDE_MODEL` remains supported as a
back-compatible alias for the Anthropic balanced-tier model.
