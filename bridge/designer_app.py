"""FastAPI app for the Wallac Victor2 designer/Run Builder backend.

Implements Stage 3 of docs/plans/wallac-protocol-authoring.md.

Exposes authenticated HTTP endpoints for:
- Creating, reading, updating, and listing draft Method/Layout/Analysis/Job
  objects.
- Finalizing a draft (canonicalize + attach JSON + write hash to metadata).
- Cloning a signed object to a new draft.

The browser never receives the eLabFTW API key or vm-agent token.  All
eLabFTW interaction happens server-side through the designer service.

Authentication: a bearer token compared against ``WALLAC_DESIGNER_TOKEN``.
If the token is unset, auth is disabled (dev mode only).
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .config import BridgeConfig
from .designer import ATTACHMENT_NAMES, DesignerService, DraftObject
from .elabftw import ElabftwClient
from .errors import BridgeError
from .security_headers import install_security_headers

# --- Pydantic models for request/response ---


class DraftCreateRequest(BaseModel):
    title: str = Field(..., description="Human-readable title for the eLabFTW item")
    spec: dict[str, Any] = Field(default_factory=dict, description="Spec dict")


class DraftUpdateRequest(BaseModel):
    spec: dict[str, Any] = Field(..., description="Updated spec dict")


class CloneRequest(BaseModel):
    new_title: str = Field(..., description="Title for the new draft clone")


class DraftResponse(BaseModel):
    item_id: int
    title: str
    category_id: int
    lifecycle: str
    spec: dict[str, Any]
    hash: str
    json_attachment_id: int


class FinalizeResponse(BaseModel):
    item_id: int
    hash: str
    json_attachment_id: int
    filename: str


def _draft_to_response(d: DraftObject) -> DraftResponse:
    return DraftResponse(
        item_id=d.item_id,
        title=d.title,
        category_id=d.category_id,
        lifecycle=d.lifecycle,
        spec=d.spec_dict,
        hash=d.hash,
        json_attachment_id=d.json_attachment_id,
    )


def _bridge_error_to_http(e: BridgeError) -> HTTPException:
    """Map a BridgeError to an HTTPException with appropriate status code."""
    status_code = status.HTTP_400_BAD_REQUEST
    if e.code in ("signature_missing", "signature_invalid", "signer_unauthorized"):
        status_code = status.HTTP_403_FORBIDDEN
    elif e.code == "operator_review_required":
        status_code = status.HTTP_409_CONFLICT
    return HTTPException(
        status_code=status_code,
        detail={
            "code": e.code,
            "human_message": e.human_message,
            "operator_hint": e.operator_hint,
            "retryable": e.retryable,
            "details": e.details,
        },
    )


# --- Auth dependency factory ---


def _make_auth_check(token: str) -> Callable[..., None]:
    """Return a FastAPI dependency that checks the bearer token.

    Uses ``hmac.compare_digest`` for constant-time comparison; ``compare_digest``
    returns False (without raising) when lengths differ, so no guard is needed.
    """

    def _check_auth(authorization: str | None = Header(default=None)) -> None:
        if not token:
            return  # dev mode: no auth
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid Authorization header",
            )
        presented = authorization.removeprefix("Bearer ")
        if not hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            )

    return _check_auth


# --- CRUD endpoint registration ---


def _register_crud_endpoints(
    app: FastAPI,
    service: DesignerService,
    kind: str,
    path_prefix: str,
    auth_dep: Callable[..., None],
) -> None:
    """Register CRUD + finalize + clone endpoints for a given object kind.

    Args:
        app: The FastAPI app to register routes on.
        service: The DesignerService instance.
        kind: One of "method", "layout", "analysis", "job".
        path_prefix: URL path prefix, e.g. "/api/methods".
        auth_dep: FastAPI dependency for auth checking.
    """
    filename = ATTACHMENT_NAMES[kind]
    singular = path_prefix.rstrip("s").rsplit("/", 1)[-1]

    def _create(req: DraftCreateRequest) -> DraftResponse:
        try:
            draft = service.create_draft(kind, req.title, req.spec)
            return _draft_to_response(draft)
        except BridgeError as e:
            raise _bridge_error_to_http(e) from e

    def _list() -> list[DraftResponse]:
        return [_draft_to_response(d) for d in service.list_drafts(kind)]

    def _get(item_id: int) -> DraftResponse:
        try:
            draft = service.get_draft(kind, item_id)
            return _draft_to_response(draft)
        except BridgeError as e:
            raise _bridge_error_to_http(e) from e

    def _update(item_id: int, req: DraftUpdateRequest) -> DraftResponse:
        try:
            draft = service.update_draft(kind, item_id, req.spec)
            return _draft_to_response(draft)
        except BridgeError as e:
            raise _bridge_error_to_http(e) from e

    def _finalize(item_id: int) -> FinalizeResponse:
        try:
            draft = service.finalize_draft(kind, item_id)
            return FinalizeResponse(
                item_id=draft.item_id,
                hash=draft.hash,
                json_attachment_id=draft.json_attachment_id,
                filename=filename,
            )
        except BridgeError as e:
            raise _bridge_error_to_http(e) from e

    def _clone(item_id: int, req: CloneRequest) -> DraftResponse:
        try:
            draft = service.clone_signed(kind, item_id, req.new_title)
            return _draft_to_response(draft)
        except BridgeError as e:
            raise _bridge_error_to_http(e) from e

    # Register routes with auth dependency
    app.add_api_route(
        path_prefix,
        _create,
        methods=["POST"],
        response_model=DraftResponse,
        dependencies=[Depends(auth_dep)],
        name=f"create_{singular}",
    )
    app.add_api_route(
        path_prefix,
        _list,
        methods=["GET"],
        response_model=list[DraftResponse],
        dependencies=[Depends(auth_dep)],
        name=f"list_{kind}",
    )
    app.add_api_route(
        f"{path_prefix}/{{item_id}}",
        _get,
        methods=["GET"],
        response_model=DraftResponse,
        dependencies=[Depends(auth_dep)],
        name=f"get_{singular}",
    )
    app.add_api_route(
        f"{path_prefix}/{{item_id}}",
        _update,
        methods=["PATCH"],
        response_model=DraftResponse,
        dependencies=[Depends(auth_dep)],
        name=f"update_{singular}",
    )
    app.add_api_route(
        f"{path_prefix}/{{item_id}}/finalize",
        _finalize,
        methods=["POST"],
        response_model=FinalizeResponse,
        dependencies=[Depends(auth_dep)],
        name=f"finalize_{singular}",
    )
    app.add_api_route(
        f"{path_prefix}/{{item_id}}/clone",
        _clone,
        methods=["POST"],
        response_model=DraftResponse,
        dependencies=[Depends(auth_dep)],
        name=f"clone_{singular}",
    )


def _register_elabftw_proxy(
    app: FastAPI,
    config: Any,
    auth_dep: Callable[..., None],
) -> None:
    """Register a proxy endpoint for eLabFTW API calls.

    The Run Builder is served over HTTP but eLabFTW uses HTTPS with a
    self-signed cert. Browsers block cross-origin fetches to HTTPS with
    untrusted certs. This proxy lets the browser call same-origin HTTP.
    """
    import json as _json
    import ssl
    import urllib.error
    import urllib.parse
    import urllib.request

    @app.get("/elabftw/events", dependencies=[Depends(auth_dep)])
    def get_elabftw_events(items_id: int, start: str = "", end: str = "") -> list:
        if not config:
            raise HTTPException(status_code=503, detail="No eLabFTW config")

        # Reason: build the query string via urlencode so any characters in
        # `start`/`end` are percent-encoded and cannot inject additional query
        # parameters or path segments into the eLabFTW URL. ``items_id`` is
        # typed as ``int`` and the base URL comes from server config, so the
        # only user-controllable input is the query string, which we now
        # encode. This addresses the CodeQL ``py/partial-ssrf`` finding at
        # designer_app.py:258.
        query: dict[str, str | int] = {"items_id": items_id}
        if start:
            query["start"] = start
        if end:
            query["end"] = end

        url = f"{config.elabftw_url}/api/v2/events?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url)  # noqa: S310  # Base URL is server config.
        req.add_header("Authorization", config.elabftw_api_key)

        ctx = ssl.create_default_context()
        if not config.elabftw_verify_tls:
            # Operator opt-out for self-signed eLabFTW certs. Set
            # WALLAC_ELABFTW_VERIFY_TLS=0 in deploy/bridge.env.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            with urllib.request.urlopen(req, context=ctx) as resp:  # noqa: S310
                return _json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise HTTPException(status_code=e.code, detail=str(e.read().decode()[:200])) from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"eLabFTW unreachable: {e}") from e


# --- App factory ---


def create_designer_app(
    config: BridgeConfig | None = None,
    service: DesignerService | None = None,
) -> FastAPI:
    """Create the FastAPI designer app.

    Args:
        config: Bridge config (for production). If None, must provide service.
        service: Pre-configured DesignerService (for testing). If None, one is
            built from config.
    """
    if service is None:
        if config is None:
            config = BridgeConfig.from_env()
        client = ElabftwClient(
            base_url=config.elabftw_url,
            api_key=config.elabftw_api_key,
            verify_tls=config.elabftw_verify_tls,
        )
        service = DesignerService(client)

    designer_token = os.environ.get("WALLAC_DESIGNER_TOKEN", "")

    # Strict-auth mode: refuse to start with empty designer token.
    if config is not None and config.require_auth and not designer_token:
        raise RuntimeError(
            "WALLAC_REQUIRE_AUTH is set but WALLAC_DESIGNER_TOKEN is empty; "
            "refusing to start the designer with auth disabled. "
            "Either unset WALLAC_REQUIRE_AUTH or set WALLAC_DESIGNER_TOKEN."
        )

    auth_dep = _make_auth_check(designer_token)

    app = FastAPI(
        title="Wallac Victor2 Designer",
        description="Authenticated draft APIs for protocol authoring",
        version="0.1.0",
    )

    install_security_headers(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/config", dependencies=[Depends(auth_dep)])
    def get_config() -> dict[str, str]:
        """Return client-side config (URLs) so the Run Builder can auto-fill.

        This endpoint is now behind the same bearer-token check as the rest
        of the designer API and omits the internal ``vm_agent_url`` (which
        was leaking the libvirt NAT address). See ``SECURITY.md``.
        """
        import os

        return {
            "elabftw_url": config.elabftw_url
            if config
            else os.environ.get("WALLAC_ELABFTW_URL", ""),
            "bridge_url": os.environ.get("WALLAC_BRIDGE_URL", ""),
        }

    _register_elabftw_proxy(app, config, auth_dep)

    @app.get("/run-builder")
    def run_builder() -> Any:
        """Serve the Run Builder single-page app."""
        from pathlib import Path

        html_path = Path(__file__).parent / "run_builder.html"
        if not html_path.exists():
            raise HTTPException(status_code=404, detail="run_builder.html not found")
        from fastapi.responses import HTMLResponse

        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    # Register CRUD endpoints for all four object kinds
    for kind, prefix in [
        ("method", "/api/methods"),
        ("layout", "/api/layouts"),
        ("analysis", "/api/analyses"),
        ("job", "/api/jobs"),
    ]:
        _register_crud_endpoints(app, service, kind, prefix, auth_dep)

    return app
