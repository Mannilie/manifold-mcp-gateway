"""Shared Unraid plumbing (DECISIONS.md, Phase 5 gate 1). One GraphQL client owns the URL,
the x-api-key header, retries and error mapping. Toolsets never build a request."""

from manifold.unraid.client import UnraidClient, UnraidError, UnraidSchemaError, client_for

__all__ = ["UnraidClient", "UnraidError", "UnraidSchemaError", "client_for"]
