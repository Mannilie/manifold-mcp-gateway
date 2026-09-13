"""Request guards for /api (SPEC.md sections 9 and 13, DECISIONS.md Phase 3 gate 1)."""

from __future__ import annotations

from fastapi import HTTPException, Request

from manifold.auth.access import access_email_from_scope

CSRF_HEADER = "x-manifold-request"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def require_admin(request: Request) -> str:
    """The Cloudflare Access identity, checked against MANIFOLD_ADMIN_EMAILS. Mutating
    requests must also carry X-Manifold-Request, which a cross-origin form cannot add."""
    email = access_email_from_scope(request.scope)
    if email is None:
        raise HTTPException(status_code=401, detail="no Cloudflare Access identity")
    if email not in request.app.state.settings.admin_emails:
        raise HTTPException(status_code=403, detail="not an admin")
    if request.method in MUTATING and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status_code=403, detail=f"missing {CSRF_HEADER} header")
    return email
