"""What each provider is, what it needs, and whether it actually works here.

This module is the single source of truth behind the "AI providers" screen. If
a provider is listed as available in the UI, `implemented` is True here and
there is a class behind it that makes real HTTP calls. Nothing is listed as
"coming soon" while pretending to be connectable.

Model names are **defaults, not a supported-model list**. Vendors ship models
faster than any hardcoded list can track, so a customer may type any model name
and the provider passes it through; these are only what a tier falls back to
when nobody has chosen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMProvider, ProviderConfig, ProviderNotImplemented, UnimplementedProvider
from app.llm.google_provider import GoogleProvider
from app.llm.openai_provider import (
    AzureOpenAIProvider,
    DeepSeekProvider,
    GroqProvider,
    MistralProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    TogetherProvider,
)


class BedrockProvider(UnimplementedProvider):
    """AWS Bedrock.

    Not implemented. Bedrock requires SigV4 request signing, which means
    `boto3`/`botocore` in the image and an AWS credential chain at runtime.
    Hand-rolling the signature is exactly the kind of security-sensitive code
    that should not ship untested against the real service, so this raises
    instead. Use the Anthropic or Google provider directly, or an
    OpenAI-compatible gateway in front of Bedrock.
    """

    kind = "BEDROCK"
    reason = (
        "AWS Bedrock is not implemented in this deployment. It requires SigV4 "
        "request signing and the AWS credential chain. Use the Anthropic or "
        "Google provider directly, or point the OpenAI-compatible provider at a "
        "gateway in front of Bedrock."
    )


class VertexProvider(UnimplementedProvider):
    """Google Vertex AI.

    Not implemented. Vertex authenticates with a Google service-account
    assertion rather than an API key, which is a different credential lifecycle
    from everything else here. The Google provider covers Gemini through the
    Generative Language API with a plain API key.
    """

    kind = "VERTEX"
    reason = (
        "Google Vertex AI is not implemented in this deployment. It requires "
        "service-account credentials rather than an API key. Use the Google "
        "provider, which reaches the same Gemini models with an API key."
    )


@dataclass(frozen=True)
class ProviderSpec:
    kind: str
    label: str
    cls: type[LLMProvider]
    implemented: bool
    #: What the operator must supply. Rendered as the credential form.
    requires_api_key: bool = True
    requires_base_url: bool = False
    #: Non-secret settings this provider needs, e.g. Azure's deployment name.
    config_fields: tuple[str, ...] = ()
    #: Tier -> default model when nobody has chosen one.
    default_models: dict[str, str] = field(default_factory=dict)
    supports_tools: bool = True
    notes: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "implemented": self.implemented,
            "requires_api_key": self.requires_api_key,
            "requires_base_url": self.requires_base_url,
            "config_fields": list(self.config_fields),
            "default_models": dict(self.default_models),
            "supports_tools": self.supports_tools,
            "notes": self.notes,
        }


PROVIDERS: dict[str, ProviderSpec] = {
    "ANTHROPIC": ProviderSpec(
        kind="ANTHROPIC",
        label="Anthropic",
        cls=AnthropicProvider,
        implemented=True,
        default_models={
            "FAST": "claude-haiku-4-5",
            "BALANCED": "claude-sonnet-4-5",
            "ADVANCED": "claude-opus-4-1",
        },
        notes="Native tool use. The canonical message shape of this platform.",
    ),
    "OPENAI": ProviderSpec(
        kind="OPENAI",
        label="OpenAI",
        cls=OpenAIProvider,
        implemented=True,
        config_fields=("organization",),
        default_models={"FAST": "gpt-4o-mini", "BALANCED": "gpt-4o", "ADVANCED": "gpt-4o"},
    ),
    "AZURE_OPENAI": ProviderSpec(
        kind="AZURE_OPENAI",
        label="Azure OpenAI",
        cls=AzureOpenAIProvider,
        implemented=True,
        requires_base_url=True,
        config_fields=("deployment", "api_version"),
        notes=(
            "Azure addresses a deployment, not a model: enter your deployment "
            "name where a model name is asked for."
        ),
    ),
    "GOOGLE": ProviderSpec(
        kind="GOOGLE",
        label="Google Gemini",
        cls=GoogleProvider,
        implemented=True,
        default_models={
            "FAST": "gemini-2.0-flash",
            "BALANCED": "gemini-2.5-flash",
            "ADVANCED": "gemini-2.5-pro",
        },
        notes="Generative Language API with an API key.",
    ),
    "MISTRAL": ProviderSpec(
        kind="MISTRAL",
        label="Mistral",
        cls=MistralProvider,
        implemented=True,
        default_models={
            "FAST": "mistral-small-latest",
            "BALANCED": "mistral-medium-latest",
            "ADVANCED": "mistral-large-latest",
        },
    ),
    "GROQ": ProviderSpec(
        kind="GROQ",
        label="Groq",
        cls=GroqProvider,
        implemented=True,
    ),
    "DEEPSEEK": ProviderSpec(
        kind="DEEPSEEK",
        label="DeepSeek",
        cls=DeepSeekProvider,
        implemented=True,
        default_models={"FAST": "deepseek-chat", "BALANCED": "deepseek-chat"},
    ),
    "TOGETHER": ProviderSpec(
        kind="TOGETHER",
        label="Together AI",
        cls=TogetherProvider,
        implemented=True,
    ),
    "OLLAMA": ProviderSpec(
        kind="OLLAMA",
        label="Ollama (self-hosted)",
        cls=OllamaProvider,
        implemented=True,
        requires_api_key=False,
        requires_base_url=True,
        notes=(
            "Runs entirely on your own infrastructure. Tool calling depends on "
            "the model you have pulled — verify it with Test connection before "
            "relying on it."
        ),
    ),
    "OPENAI_COMPATIBLE": ProviderSpec(
        kind="OPENAI_COMPATIBLE",
        label="OpenAI-compatible endpoint",
        cls=OpenAICompatibleProvider,
        implemented=True,
        requires_base_url=True,
        notes="Any gateway or proxy that speaks the OpenAI chat-completions API.",
    ),
    "BEDROCK": ProviderSpec(
        kind="BEDROCK",
        label="AWS Bedrock",
        cls=BedrockProvider,
        implemented=False,
        notes=BedrockProvider.reason,
    ),
    "VERTEX": ProviderSpec(
        kind="VERTEX",
        label="Google Vertex AI",
        cls=VertexProvider,
        implemented=False,
        notes=VertexProvider.reason,
    ),
}


def spec_for(kind: str) -> ProviderSpec:
    spec = PROVIDERS.get(str(kind).upper())
    if spec is None:
        raise ProviderNotImplemented(
            f"'{kind}' is not a provider this platform knows about.", provider=str(kind)
        )
    return spec


def build_provider(kind: str, config: ProviderConfig) -> LLMProvider:
    """Instantiate a provider. Unimplemented kinds raise here, not later."""
    return spec_for(kind).cls(config)


def default_model(kind: str, tier: str) -> str:
    spec = spec_for(kind)
    return spec.default_models.get(tier.upper()) or spec.default_models.get("BALANCED", "")


def catalog() -> list[dict[str, Any]]:
    """Everything the AI settings screen needs, implemented ones first."""
    return [
        spec.describe()
        for spec in sorted(
            PROVIDERS.values(), key=lambda s: (not s.implemented, s.label.lower())
        )
    ]
