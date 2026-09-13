"""Shared Google plumbing for every Google toolset (DECISIONS.md, Phase 4 gate 1).

One client, one place for URLs, Authorization headers, retries and rate limits, and one
error mapping. Toolsets call `client_for(credentials, scopes)` and then `get`, `post`,
`put` or `delete` with a relative path. They never build a request themselves.
"""

from manifold.google.client import GoogleClient, client_for
from manifold.google.errors import GoogleError

__all__ = ["GoogleClient", "GoogleError", "client_for"]
