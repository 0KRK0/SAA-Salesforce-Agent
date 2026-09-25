"""Identity provider abstraction (Phase L).

Honest status, because this is the part of an enterprise product people most
often fake:

  * **LOCAL** — fully implemented. Signed JWT sessions over the users table.
  * **OIDC** — fully implemented: discovery, authorization-code flow with
    PKCE and state, code exchange, and ID-token signature/claim verification
    against the provider's JWKS. Turn it on by setting OIDC_ISSUER,
    OIDC_CLIENT_ID and OIDC_CLIENT_SECRET.
  * **SCIM 2.0 user provisioning** — implemented for the Users resource
    (create, list, replace, patch-active, delete) in app/api/routes_scim.py.
  * **SAML** — NOT implemented. Adding it means an XML-signature dependency
    (xmlsec) and an assertion-validation path that must not be improvised.
    `SamlProvider` below raises rather than pretending; the abstraction is
    here so adding it does not touch the agent, and nothing in the product
    reports SAML as available.

Every provider ends at the same place: a `ResolvedIdentity`, which the session
layer turns into a user + membership. Authorization never depends on which
provider authenticated the human.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import JWTError

from app.config import settings
from app.models import IdentityProviderKind
from app.observability.logging import get_logger

log = get_logger("security.identity")


class IdentityError(RuntimeError):
    """Authentication failed at the provider boundary."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class ProviderNotConfigured(IdentityError):
    pass


class ProviderNotImplemented(IdentityError):
    pass


@dataclass(frozen=True)
class ResolvedIdentity:
    """What every provider must produce, and all the session layer consumes."""

    subject: str
    email: str
    display_name: str = ""
    kind: IdentityProviderKind = IdentityProviderKind.LOCAL
    # Raw claims, kept for audit. Never placed in a prompt.
    claims: dict[str, Any] | None = None


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    state: str
    code_verifier: str
    nonce: str


class IdentityProvider:
    kind: IdentityProviderKind = IdentityProviderKind.LOCAL
    name = "local"

    @property
    def configured(self) -> bool:  # pragma: no cover - trivial
        return True

    async def begin(self, redirect_after: str = "") -> AuthorizationRequest:
        raise ProviderNotImplemented(
            f"{self.name} does not use a redirect-based login flow."
        )

    async def complete(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> ResolvedIdentity:
        raise ProviderNotImplemented(f"{self.name} does not implement a callback.")


class LocalProvider(IdentityProvider):
    """Email-identified sessions signed with the deployment's session secret.

    Real, not a stand-in: the session is a signed JWT verified on every
    request. It is appropriate for single-tenant and pilot deployments; an
    enterprise rollout should configure OIDC and disable local login at the
    reverse proxy.
    """

    kind = IdentityProviderKind.LOCAL
    name = "local"

    async def resolve(self, email: str, display_name: str = "") -> ResolvedIdentity:
        normalized = email.strip().lower()
        if "@" not in normalized:
            raise IdentityError("A valid email address is required.")
        return ResolvedIdentity(
            subject=normalized,
            email=normalized,
            display_name=display_name or normalized.split("@")[0],
            kind=IdentityProviderKind.LOCAL,
        )


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class OidcProvider(IdentityProvider):
    """OpenID Connect authorization-code flow with PKCE.

    The ID token is verified — signature against the provider's JWKS, plus
    issuer, audience, expiry and nonce — before any identity is trusted. An
    unverifiable token is an authentication failure, never a warning.
    """

    kind = IdentityProviderKind.OIDC
    name = "oidc"

    def __init__(self) -> None:
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at: float = 0.0

    @property
    def configured(self) -> bool:
        return settings.oidc_configured

    def _require_configured(self) -> None:
        if not self.configured:
            raise ProviderNotConfigured(
                "OIDC is not configured. Set OIDC_ISSUER, OIDC_CLIENT_ID and "
                "OIDC_CLIENT_SECRET to enable enterprise single sign-on."
            )

    async def discovery(self) -> dict[str, Any]:
        self._require_configured()
        if self._discovery is not None:
            return self._discovery
        url = settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
        if resp.status_code >= 300:
            raise IdentityError(
                f"OIDC discovery failed ({resp.status_code}) at {url}.", retryable=True
            )
        self._discovery = resp.json()
        return self._discovery

    async def jwks(self) -> dict[str, Any]:
        # Keys rotate; re-fetch hourly and on an unknown kid.
        if self._jwks is not None and time.time() - self._jwks_fetched_at < 3600:
            return self._jwks
        doc = await self.discovery()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(doc["jwks_uri"])
        if resp.status_code >= 300:
            raise IdentityError("Could not fetch the OIDC signing keys.", retryable=True)
        self._jwks = resp.json()
        self._jwks_fetched_at = time.time()
        return self._jwks

    async def begin(self, redirect_after: str = "") -> AuthorizationRequest:
        doc = await self.discovery()
        verifier = _b64url(os.urandom(48))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = _b64url(os.urandom(24))
        nonce = _b64url(os.urandom(24))
        params = {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": settings.oidc_redirect_uri,
            "scope": " ".join(settings.oidc_scope_list),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        url = httpx.URL(doc["authorization_endpoint"], params=params)
        return AuthorizationRequest(
            url=str(url), state=state, code_verifier=verifier, nonce=nonce
        )

    async def complete(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> ResolvedIdentity:
        doc = await self.discovery()
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                doc["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.oidc_redirect_uri,
                    "client_id": settings.oidc_client_id,
                    "client_secret": settings.oidc_client_secret,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code >= 300:
            raise IdentityError(f"OIDC token exchange failed: {resp.text[:300]}")
        payload = resp.json()
        id_token = payload.get("id_token")
        if not id_token:
            raise IdentityError("The OIDC provider returned no id_token.")

        claims = await self._verify(id_token, nonce)
        email = str(claims.get("email") or "").strip().lower()
        if not email:
            raise IdentityError(
                "The OIDC id_token carried no email claim; request the 'email' scope."
            )
        if claims.get("email_verified") is False:
            raise IdentityError("The identity provider reports this email as unverified.")
        return ResolvedIdentity(
            subject=str(claims.get("sub")),
            email=email,
            display_name=str(claims.get("name") or email.split("@")[0]),
            kind=IdentityProviderKind.OIDC,
            claims=_retained_claims(claims),
        )

    async def _verify(self, id_token: str, nonce: str) -> dict[str, Any]:
        keys = await self.jwks()
        try:
            claims = jwt.decode(
                id_token,
                keys,
                audience=settings.oidc_client_id,
                issuer=settings.oidc_issuer.rstrip("/"),
                options={"verify_at_hash": False},
            )
        except JWTError:
            # A rotated key is the common benign cause; retry once with fresh keys.
            self._jwks = None
            keys = await self.jwks()
            try:
                claims = jwt.decode(
                    id_token,
                    keys,
                    audience=settings.oidc_client_id,
                    issuer=settings.oidc_issuer.rstrip("/"),
                    options={"verify_at_hash": False},
                )
            except JWTError as exc:
                raise IdentityError(f"The OIDC id_token failed verification: {exc}") from exc
        if nonce and claims.get("nonce") != nonce:
            raise IdentityError("OIDC nonce mismatch; the login could not be trusted.")
        return claims


#: Claims kept from a verified id_token. Everything else is discarded on the
#: spot: an id_token can carry a great deal about a person that this product
#: has no business holding, and the safest place for it is nowhere.
#:
#: Group claims ARE retained, because group-to-role mapping is a security
#: control and cannot run without them. `app.security.mapping.GROUP_CLAIMS`
#: lists the names, and the two lists are kept in step deliberately.
RETAINED_CLAIMS = (
    "sub",
    "iss",
    "aud",
    "email",
    "name",
    "groups",
    "roles",
    "memberOf",
    "https://claims/groups",
)


def _retained_claims(claims: dict[str, Any]) -> dict[str, Any]:
    return {k: claims[k] for k in RETAINED_CLAIMS if k in claims}


class SamlProvider(IdentityProvider):
    """Deliberately unimplemented.

    SAML needs XML-signature verification (xmlsec) and assertion replay
    protection. Approximating either would be worse than not shipping it, so
    this raises and nothing in the product advertises SAML as available.
    """

    kind = IdentityProviderKind.SAML
    name = "saml"

    @property
    def configured(self) -> bool:
        return False

    async def begin(self, redirect_after: str = "") -> AuthorizationRequest:
        raise ProviderNotImplemented(
            "SAML single sign-on is not implemented in this build. Use OIDC, or open "
            "an issue describing the IdP you need."
        )

    async def complete(self, **_: Any) -> ResolvedIdentity:
        raise ProviderNotImplemented("SAML single sign-on is not implemented in this build.")


local_provider = LocalProvider()
oidc_provider = OidcProvider()
saml_provider = SamlProvider()


def describe_providers() -> dict[str, Any]:
    """What this deployment can actually authenticate with, stated plainly."""
    return {
        "providers": [
            {
                "name": "local",
                "kind": "LOCAL",
                "available": True,
                "implemented": True,
                "note": "Signed sessions over the local users table.",
            },
            {
                "name": "oidc",
                "kind": "OIDC",
                "available": oidc_provider.configured,
                "implemented": True,
                "note": (
                    "Authorization-code flow with PKCE and full id_token verification."
                    if oidc_provider.configured
                    else "Implemented, but OIDC_ISSUER/CLIENT_ID/CLIENT_SECRET are unset."
                ),
            },
            {
                "name": "saml",
                "kind": "SAML",
                "available": False,
                "implemented": False,
                "note": "Not implemented in this build.",
            },
        ],
        "scim": {
            "available": settings.scim_configured,
            "implemented": True,
            "resources": ["Users"],
            "note": (
                "SCIM 2.0 Users provisioning is enabled."
                if settings.scim_configured
                else "Implemented, but SCIM_BEARER_TOKEN is unset."
            ),
        },
    }
