"""OAuth endpoints and the bearer wrapper for toolset transports.

Issuer is the bare origin, metadata lives at `/.well-known/oauth-authorization-server`, and
the endpoints sit under `/oauth/`. RFC 8414 does not require endpoints under the issuer path,
and a path-less issuer is what every client discovers first.
"""

from __future__ import annotations

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.routes import build_resource_metadata_url, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE, RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from pydantic import AnyHttpUrl
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route, request_response
from starlette.types import ASGIApp

from manifold.auth.access import AccessGate
from manifold.auth.guard import (
    DEFAULT_MAX_CLIENTS,
    DEFAULT_MAX_PER_WINDOW,
    DEFAULT_WINDOW_SECONDS,
    RegistrationGuard,
)
from manifold.auth.provider import ManifoldOAuthProvider, ToolsetTokenVerifier

AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"
REGISTER_PATH = "/oauth/register"
REVOKE_PATH = "/oauth/revoke"
METADATA_PATH = "/.well-known/oauth-authorization-server"
PROTECTED_RESOURCE_PREFIX = "/.well-known/oauth-protected-resource"


def build_metadata(base_url: str) -> OAuthMetadata:
    return OAuthMetadata(
        issuer=base_url,
        authorization_endpoint=f"{base_url}{AUTHORIZE_PATH}",
        token_endpoint=f"{base_url}{TOKEN_PATH}",
        registration_endpoint=f"{base_url}{REGISTER_PATH}",
        revocation_endpoint=f"{base_url}{REVOKE_PATH}",
        revocation_endpoint_auth_methods_supported=["client_secret_post", "client_secret_basic"],
        response_types_supported=["code"],
        grant_types_supported=["authorization_code", "refresh_token"],
        token_endpoint_auth_methods_supported=["client_secret_post", "client_secret_basic", "none"],
        code_challenge_methods_supported=["S256"],
    )


def build_oauth_routes(
    provider: ManifoldOAuthProvider,
    base_url: str,
    admin_emails: frozenset[str],
    max_registrations_per_window: int = DEFAULT_MAX_PER_WINDOW,
    registration_window_seconds: float = DEFAULT_WINDOW_SECONDS,
    max_clients: int = DEFAULT_MAX_CLIENTS,
) -> list[Route]:
    authenticator = ClientAuthenticator(provider)
    register = cors_middleware(
        RegistrationHandler(provider, options=ClientRegistrationOptions(enabled=True)).handle,
        ["POST", "OPTIONS"],
    )

    def limited(app: ASGIApp) -> ASGIApp:
        return RequestBodyLimitMiddleware(app, DEFAULT_MAX_REQUEST_BODY_SIZE)

    return [
        Route(
            METADATA_PATH,
            endpoint=cors_middleware(
                MetadataHandler(build_metadata(base_url)).handle, ["GET", "OPTIONS"]
            ),
            methods=["GET", "OPTIONS"],
        ),
        Route(
            AUTHORIZE_PATH,
            endpoint=AccessGate(
                limited(request_response(AuthorizationHandler(provider).handle)), admin_emails
            ),
            methods=["GET", "POST"],
        ),
        Route(
            TOKEN_PATH,
            endpoint=cors_middleware(
                TokenHandler(provider, authenticator).handle, ["POST", "OPTIONS"]
            ),
            methods=["POST", "OPTIONS"],
        ),
        Route(
            REGISTER_PATH,
            endpoint=RegistrationGuard(
                register,
                provider.store,
                max_per_window=max_registrations_per_window,
                window_seconds=registration_window_seconds,
                max_clients=max_clients,
            ),
            methods=["POST", "OPTIONS"],
        ),
        Route(
            REVOKE_PATH,
            endpoint=cors_middleware(
                RevocationHandler(provider, authenticator).handle, ["POST", "OPTIONS"]
            ),
            methods=["POST", "OPTIONS"],
        ),
    ]


def protected_resource_metadata(base_url: str, key: str, display_name: str) -> dict[str, object]:
    metadata = ProtectedResourceMetadata(
        resource=f"{base_url}/{key}",
        authorization_servers=[base_url],
        resource_name=display_name,
    )
    return metadata.model_dump(mode="json", exclude_none=True)


class BearerProtector:
    """Wraps a toolset's transport so it requires a Manifold-issued bearer token."""

    def __init__(self, provider: ManifoldOAuthProvider, base_url: str) -> None:
        self._provider = provider
        self._base_url = base_url

    def __call__(self, key: str, app: ASGIApp) -> ASGIApp:
        resource = f"{self._base_url}/{key}"
        metadata_url = build_resource_metadata_url(AnyHttpUrl(resource))
        return AuthenticationMiddleware(
            RequireAuthMiddleware(app, required_scopes=[], resource_metadata_url=metadata_url),
            backend=BearerAuthBackend(ToolsetTokenVerifier(self._provider, resource)),
        )
