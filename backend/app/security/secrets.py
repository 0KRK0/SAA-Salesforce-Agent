"""Secret storage.

Nothing in this codebase stores a credential in a column. It stores a
**reference** — an opaque string like `local:v1:…` or `awssm:prod/sfagent/…` —
and asks this module to resolve it when, and only when, an outbound call needs
it. That indirection is what makes the following true:

  * a database dump contains no usable credentials;
  * moving to a cloud KMS or Vault is a configuration change, not a migration;
  * a reference is safe to log, and the secret never is.

Two implementations ship:

  * **LocalSecretStore** — Fernet, keyed by `ENCRYPTION_KEY`. Real encryption,
    appropriate for development and single-node deployments. Its weakness is
    honest and documented: the key sits in the application's environment, so
    anything that can read the environment can decrypt the store.

  * **EnvelopeSecretStore** — the shape a KMS-backed store takes: a per-secret
    data key wrapped by a key the application never sees. `AwsKmsKeyWrapper`
    is implemented against boto3 when it is installed; `VaultKeyWrapper` and
    `AzureKeyVaultKeyWrapper` raise `KeyWrapperNotImplemented` rather than
    pretending. Selecting an unimplemented wrapper fails at startup, loudly,
    instead of silently falling back to something weaker.

The rule this module exists to enforce: **a resolved secret never travels
upward**. `resolve()` returns it to the adapter making the call, and that
adapter must not place it in a model prompt, a tool result, an audit row, an
API response or a log line.
"""

from __future__ import annotations

import base64
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.observability.logging import get_logger

log = get_logger("security.secrets")

#: Reference prefixes. The prefix identifies which store wrote the reference,
#: so a deployment that migrates stores can still read old references.
LOCAL_PREFIX = "local:v1:"
ENVELOPE_PREFIX = "env:v1:"
EXTERNAL_PREFIX = "ext:v1:"


class SecretError(RuntimeError):
    """A secret could not be stored or resolved."""


class SecretNotConfigured(SecretError):
    """The secret backend is not configured. Refuse rather than store plaintext."""


class KeyWrapperNotImplemented(SecretError):
    """A key wrapper was selected that this build does not implement."""


class SecretStore(ABC):
    """Stores a secret and returns an opaque reference to it."""

    name: str = "abstract"

    @abstractmethod
    def store(self, plaintext: str, *, context: dict[str, str] | None = None) -> str:
        """Persist a secret and return the reference to it."""

    @abstractmethod
    def resolve(self, reference: str, *, context: dict[str, str] | None = None) -> str:
        """Return the secret for a reference. Never log or return this upward."""

    def rotate(
        self, reference: str, plaintext: str, *, context: dict[str, str] | None = None
    ) -> str:
        """Replace a secret, returning a new reference. Callers store the new one."""
        return self.store(plaintext, context=context)

    def delete(self, reference: str) -> None:
        """Best-effort destruction. Self-contained references have nothing to delete."""
        return None

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "implemented": True}


# ---------------------------------------------------------------------------
# Local Fernet store
# ---------------------------------------------------------------------------
class LocalSecretStore(SecretStore):
    """Fernet-encrypted, self-contained references.

    The ciphertext travels inside the reference, so there is no second store to
    keep in sync. `context` is bound in as additional authenticated data by
    prefixing it into the plaintext envelope, which means a reference stolen
    from one project's row cannot be resolved as another's.
    """

    name = "local-fernet"

    def _key(self) -> bytes:
        raw = settings.encryption_key.strip()
        if not raw:
            raise SecretNotConfigured(
                "ENCRYPTION_KEY is not set, so secrets cannot be encrypted. "
                "Generate one with: python -c \"from cryptography.fernet import "
                'Fernet; print(Fernet.generate_key().decode())"'
            )
        try:
            key = raw.encode()
            Fernet(key)
            return key
        except Exception as exc:
            raise SecretNotConfigured(
                "ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64 bytes)."
            ) from exc

    @staticmethod
    def _envelope(plaintext: str, context: dict[str, str] | None) -> bytes:
        return json.dumps(
            {"c": context or {}, "v": plaintext}, separators=(",", ":")
        ).encode()

    def store(self, plaintext: str, *, context: dict[str, str] | None = None) -> str:
        if not plaintext:
            raise SecretError("Refusing to store an empty secret.")
        token = Fernet(self._key()).encrypt(self._envelope(plaintext, context))
        return LOCAL_PREFIX + token.decode()

    def resolve(self, reference: str, *, context: dict[str, str] | None = None) -> str:
        if not reference or not reference.startswith(LOCAL_PREFIX):
            raise SecretError("Not a local secret reference.")
        token = reference[len(LOCAL_PREFIX) :].encode()
        try:
            raw = Fernet(self._key()).decrypt(token)
        except InvalidToken as exc:
            raise SecretError(
                "The secret could not be decrypted. The encryption key has "
                "probably changed; the credential must be re-entered."
            ) from exc
        payload = json.loads(raw)
        bound = payload.get("c") or {}
        if context:
            for key, value in context.items():
                if bound.get(key) not in (None, value):
                    # A reference lifted from another project's row.
                    raise SecretError(
                        "This secret reference does not belong to the requesting "
                        "context and was refused."
                    )
        return str(payload.get("v", ""))


# ---------------------------------------------------------------------------
# Envelope encryption
# ---------------------------------------------------------------------------
class KeyWrapper(ABC):
    """Wraps and unwraps a per-secret data key using a key we never hold."""

    name: str = "abstract"
    implemented: bool = False

    @abstractmethod
    def wrap(self, data_key: bytes, context: dict[str, str] | None) -> str: ...

    @abstractmethod
    def unwrap(self, wrapped: str, context: dict[str, str] | None) -> bytes: ...


class AwsKmsKeyWrapper(KeyWrapper):
    """AWS KMS envelope encryption.

    Implemented, but only usable where boto3 and KMS credentials are present.
    Absent either, construction fails rather than degrading to local keys.
    """

    name = "aws-kms"
    implemented = True

    def __init__(self, key_id: str, region: str | None = None):
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - depends on install
            raise KeyWrapperNotImplemented(
                "AWS KMS was selected as the secret key wrapper but boto3 is not "
                "installed. Install boto3, or set SECRET_BACKEND=local."
            ) from exc
        if not key_id:
            raise SecretNotConfigured("SECRET_KMS_KEY_ID is required for AWS KMS.")
        self._client = boto3.client("kms", region_name=region or settings.secret_kms_region)
        self._key_id = key_id

    def wrap(self, data_key: bytes, context: dict[str, str] | None) -> str:
        response = self._client.encrypt(
            KeyId=self._key_id,
            Plaintext=data_key,
            EncryptionContext=context or {},
        )
        return base64.b64encode(response["CiphertextBlob"]).decode()

    def unwrap(self, wrapped: str, context: dict[str, str] | None) -> bytes:
        response = self._client.decrypt(
            CiphertextBlob=base64.b64decode(wrapped),
            EncryptionContext=context or {},
        )
        return response["Plaintext"]


class _UnimplementedWrapper(KeyWrapper):
    """Placeholder for a backend this build does not implement.

    It raises on construction. A deployment that selects it learns immediately,
    at startup, rather than discovering at runtime that secrets were being
    written somewhere weaker than intended.
    """

    implemented = False
    guidance = ""

    def __init__(self, *_: Any, **__: Any):
        raise KeyWrapperNotImplemented(
            f"The '{self.name}' secret backend is NOT IMPLEMENTED in this build. "
            f"{self.guidance} Use SECRET_BACKEND=local or aws-kms."
        )

    def wrap(self, data_key: bytes, context: dict[str, str] | None) -> str:  # pragma: no cover
        raise KeyWrapperNotImplemented(self.name)

    def unwrap(self, wrapped: str, context: dict[str, str] | None) -> bytes:  # pragma: no cover
        raise KeyWrapperNotImplemented(self.name)


class VaultKeyWrapper(_UnimplementedWrapper):
    name = "vault"
    guidance = "It needs an hvac client and a configured transit mount."


class AzureKeyVaultKeyWrapper(_UnimplementedWrapper):
    name = "azure-key-vault"
    guidance = "It needs azure-keyvault-keys and a managed identity."


class GoogleKmsKeyWrapper(_UnimplementedWrapper):
    name = "gcp-kms"
    guidance = "It needs google-cloud-kms and workload identity."


class EnvelopeSecretStore(SecretStore):
    """Per-secret data key, wrapped by a key held outside the application.

    The reference carries the wrapped data key and the ciphertext. Compromising
    the database yields neither — decryption requires a call to the wrapper,
    which is authorized and audited by the cloud provider, not by us.
    """

    name = "envelope"

    def __init__(self, wrapper: KeyWrapper):
        self._wrapper = wrapper

    @property
    def wrapper_name(self) -> str:
        return self._wrapper.name

    def store(self, plaintext: str, *, context: dict[str, str] | None = None) -> str:
        if not plaintext:
            raise SecretError("Refusing to store an empty secret.")
        data_key = Fernet.generate_key()
        ciphertext = Fernet(data_key).encrypt(plaintext.encode())
        wrapped = self._wrapper.wrap(data_key, context)
        payload = {"k": wrapped, "c": ciphertext.decode(), "x": context or {}}
        blob = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode()
        return ENVELOPE_PREFIX + blob

    def resolve(self, reference: str, *, context: dict[str, str] | None = None) -> str:
        if not reference or not reference.startswith(ENVELOPE_PREFIX):
            raise SecretError("Not an envelope secret reference.")
        payload = json.loads(
            base64.urlsafe_b64decode(reference[len(ENVELOPE_PREFIX) :].encode())
        )
        bound = payload.get("x") or {}
        if context:
            for key, value in context.items():
                if bound.get(key) not in (None, value):
                    raise SecretError(
                        "This secret reference does not belong to the requesting "
                        "context and was refused."
                    )
        data_key = self._wrapper.unwrap(payload["k"], bound or context)
        try:
            return Fernet(data_key).decrypt(payload["c"].encode()).decode()
        except InvalidToken as exc:
            raise SecretError("The secret could not be decrypted.") from exc

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "wrapper": self._wrapper.name,
            "implemented": self._wrapper.implemented,
        }


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
_WRAPPERS: dict[str, type[KeyWrapper]] = {
    "aws-kms": AwsKmsKeyWrapper,
    "vault": VaultKeyWrapper,
    "azure-key-vault": AzureKeyVaultKeyWrapper,
    "gcp-kms": GoogleKmsKeyWrapper,
}

_store: SecretStore | None = None


def build_store(backend: str | None = None) -> SecretStore:
    backend = (backend or settings.secret_backend or "local").strip().lower()
    if backend == "local":
        return LocalSecretStore()
    wrapper_cls = _WRAPPERS.get(backend)
    if wrapper_cls is None:
        raise SecretNotConfigured(
            f"Unknown SECRET_BACKEND '{backend}'. "
            f"Supported: local, {', '.join(sorted(_WRAPPERS))}."
        )
    if backend == "aws-kms":
        return EnvelopeSecretStore(
            AwsKmsKeyWrapper(settings.secret_kms_key_id, settings.secret_kms_region)
        )
    return EnvelopeSecretStore(wrapper_cls())  # raises for unimplemented backends


def get_store() -> SecretStore:
    global _store
    if _store is None:
        _store = build_store()
    return _store


def reset_store() -> None:
    """Testing hook: drop the cached store so configuration can change."""
    global _store
    _store = None


# ---------------------------------------------------------------------------
# Convenience API used throughout the application
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SecretContext:
    """Binds a secret to where it belongs, so a stolen reference is inert."""

    company_id: str = ""
    project_id: str = ""
    purpose: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            k: v
            for k, v in (
                ("company", self.company_id),
                ("project", self.project_id),
                ("purpose", self.purpose),
            )
            if v
        }


def store_secret(plaintext: str, context: SecretContext | None = None) -> str:
    return get_store().store(
        plaintext, context=context.as_dict() if context else None
    )


def resolve_secret(reference: str, context: SecretContext | None = None) -> str:
    """Resolve a reference. The result must not travel upward — see module docstring."""
    if not reference:
        raise SecretError("No secret reference was supplied.")
    ctx = context.as_dict() if context else None
    if reference.startswith(LOCAL_PREFIX):
        return LocalSecretStore().resolve(reference, context=ctx)
    if reference.startswith(ENVELOPE_PREFIX):
        store = get_store()
        if not isinstance(store, EnvelopeSecretStore):
            raise SecretError(
                "This reference was written by an envelope secret store, but the "
                "current SECRET_BACKEND cannot read it. Restore the previous "
                "backend configuration or re-enter the credential."
            )
        return store.resolve(reference, context=ctx)
    raise SecretError("Unrecognized secret reference format.")


def fingerprint(value: str) -> str:
    """Short, non-reversible identifier for a secret, safe to log and display."""
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:16]


def masked(value: str, keep: int = 4) -> str:
    """Render a secret for display: last few characters only.

    Used where a human needs to confirm *which* key is configured without the
    key itself ever leaving the server.
    """
    if not value:
        return ""
    if len(value) <= keep:
        return "•" * len(value)
    return "•" * 8 + value[-keep:]


def describe_backend() -> dict[str, Any]:
    """What the Security page shows about how secrets are held."""
    try:
        store = get_store()
    except SecretError as exc:
        return {"backend": settings.secret_backend, "available": False, "error": str(exc)}
    detail = store.describe()
    detail["available"] = True
    if isinstance(store, LocalSecretStore):
        detail["note"] = (
            "Secrets are encrypted with a key held in the application environment. "
            "Appropriate for development and single-node deployments; use a KMS "
            "backend where the key must be outside the application."
        )
    return detail


def generate_key() -> str:
    """A fresh Fernet key, for `make key`."""
    return Fernet.generate_key().decode()


def random_token(length: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(length)).decode().rstrip("=")
