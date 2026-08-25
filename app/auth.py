"""One-user access control.

Every route that matters spends money through the Anthropic key, so a public URL
without a gate is a public URL that anyone can run up a bill on. The app's own
monthly cap bounds the damage; it does not prevent it, and it locks the owner
out for the rest of the month when it fires.

The design is deliberately the smallest thing that holds for a single user on a
phone:

  - `KT_ACCESS_KEY` is the shared secret, set as a platform secret.
  - First visit carries it once as `?k=…`; the app sets an HttpOnly, Secure,
    SameSite=Lax cookie and redirects to a clean URL so the secret does not
    linger in history, in the PWA's start_url, or in a screenshot.
  - Everything after that is the cookie.

A session token signed over a secret would be the usual next step. For one user
it buys nothing here: the cookie is HttpOnly and Secure, so the only way to read
it is to already have the device, and at that point the token would be readable
too.

Unset `KT_ACCESS_KEY` disables the gate. That is correct for local development
and wrong everywhere else, so it is announced loudly at startup.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

COOKIE = "kt_access"

# The platform's health check has no cookie and must never be gated, or the
# service is restarted forever. It exposes no data worth gating.
OPEN_PATHS = frozenset({"/api/health"})


def access_key() -> str | None:
    return os.getenv("KT_ACCESS_KEY") or None


def _ok(supplied: str | None, expected: str) -> bool:
    # compare_digest to keep the comparison time-independent of how much of the
    # key a guess got right.
    return bool(supplied) and hmac.compare_digest(supplied, expected)


async def gate(request: Request, call_next):
    expected = access_key()
    if expected is None or request.url.path in OPEN_PATHS:
        return await call_next(request)

    if _ok(request.cookies.get(COOKIE), expected):
        return await call_next(request)

    supplied = request.query_params.get("k")
    if _ok(supplied, expected):
        # Redirect to the same path without the key, so it leaves the address
        # bar, the history entry and the PWA's saved start_url.
        clean = request.url.remove_query_params("k")
        response = RedirectResponse(str(clean), status_code=303)
        response.set_cookie(
            COOKIE,
            expected,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
            max_age=60 * 60 * 24 * 365,
            path="/",
        )
        return response

    return JSONResponse(
        {"detail": "Not authorised. Open the app with ?k=<your access key> once."},
        status_code=401,
    )
