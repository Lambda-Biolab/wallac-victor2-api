"""Defense-in-depth HTTP response headers.

Adds a small, opinionated header set to every response served by the bridge
and designer apps. These are cheap belt-and-suspenders defenses — the real
security comes from bearer-token auth, pydantic input validation, and the
canonical-hash check on signed attachments. This middleware exists so that
if any of those fail, the blast radius is smaller.

Headers:

- ``Content-Security-Policy`` — restricts script/style/img/frame sources for
  the Run Builder SPA. Defense in depth against stored XSS.
- ``X-Content-Type-Options: nosniff`` — block MIME sniffing in browsers.
- ``Referrer-Policy: no-referrer`` — don't leak the bridge/designer URL as
  a Referer when the user clicks external links.
- ``X-Frame-Options: DENY`` — defense in depth for browsers that don't
  honor CSP ``frame-ancestors``.

The CSP allows inline ``<script>`` event handlers (``script-src 'self'
'unsafe-inline'``) and the inline ``<style>`` block in the Run Builder
(``style-src 'self' 'unsafe-inline'``) because refactoring to
``addEventListener`` + external CSS is out of scope here. When that
refactor lands, drop ``'unsafe-inline'``.

The ``connect-src`` directive lists ``'self'`` plus any operator-configured
``WALLAC_BRIDGE_URL`` and ``WALLAC_ELABFTW_URL`` env vars so the SPA can
reach the bridge and eLabFTW services without weakening the policy further.
Both URLs are deploy-time configuration, not request-time input.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


def _extra_connect_src() -> list[str]:
    """Read operator-configured cross-origin URLs from env.

    These are added to the CSP ``connect-src`` so the Run Builder SPA can
    reach the bridge and eLabFTW services. Both are deploy-time config.
    """
    urls: list[str] = []
    for env_name in ("WALLAC_BRIDGE_URL", "WALLAC_ELABFTW_URL"):
        url = os.environ.get(env_name, "").strip().rstrip("/")
        if url:
            urls.append(url)
    return urls


def build_csp(extra_connect_src: list[str] | None = None) -> str:
    """Build a Content-Security-Policy header value.

    ``extra_connect_src`` lists additional origins allowed in ``connect-src``,
    typically the operator-configured bridge and eLabFTW URLs.
    """
    parts = [
        "default-src 'self'",
        # Inline event handlers (``onclick="..."``) and the inline ``<style>``
        # block in the Run Builder require 'unsafe-inline'. Refactor to
        # addEventListener and external CSS to drop this.
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ]
    sources = ["'self'"]
    if extra_connect_src:
        sources.extend(extra_connect_src)
    parts.append("connect-src " + " ".join(sources))
    return "; ".join(parts)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add defense-in-depth HTTP response headers."""

    def __init__(self, app: Any, extra_connect_src: list[str] | None = None) -> None:
        super().__init__(app)
        self._csp = build_csp(
            extra_connect_src if extra_connect_src is not None else _extra_connect_src()
        )

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", self._csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response


def install_security_headers(app: Any, extra_connect_src: list[str] | None = None) -> None:
    """Install :class:`SecurityHeadersMiddleware` on a FastAPI ``app``."""
    app.add_middleware(SecurityHeadersMiddleware, extra_connect_src=extra_connect_src)
