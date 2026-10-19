"""KukHire API application factory.

``create_app`` wires settings, the database, the identity verifier and the routers. Nothing
connects at import time. The module-level ``app`` exists for ``uvicorn app.api:app``.
"""
from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Engine

from app import migrations
from app.auth import AuthKitRestVerifier, IdentityVerifier
from app.config import Settings, get_settings
from app.database import build_engine, build_session_factory
from app.routes import ats, workspace
from app.schemas import CapabilitiesResponse, Capability, HealthResponse

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _capabilities(settings: Settings) -> list[Capability]:
    return [
        Capability(key="workspaces", name="Workspaces, members and roles", status="implemented"),
        Capability(key="jobs", name="Jobs with job-specific scoring criteria", status="implemented"),
        Capability(key="candidates", name="Candidates and applications (organization-scoped)", status="implemented"),
        Capability(key="pipeline", name="Validated pipeline transitions with history", status="implemented"),
        Capability(key="scorecards", name="Interview scorecards", status="implemented"),
        Capability(
            key="hrms_handoff",
            name="Hiring handoff to HRMS",
            status="implemented" if settings.hrms_allowed_hosts else "unavailable",
            detail=None if settings.hrms_allowed_hosts else "No HRMS host is allowed (KUKHIRE_HRMS_ALLOWED_HOSTS).",
        ),
        Capability(
            key="ai_evaluation",
            name="Advisory job-specific evaluation",
            status="implemented" if settings.evaluation_enabled else "unavailable",
            detail=None if settings.evaluation_enabled else "Not enabled (KUKHIRE_EVALUATION_ENABLED).",
        ),
        Capability(
            key="email_ingestion",
            name="Email application ingestion",
            status="unavailable",
            detail="Contracts and storage exist; no mailbox connector is wired to a worker yet.",
        ),
        Capability(
            key="resume_upload_parsing",
            name="Resume file upload and parsing",
            status="unavailable",
            detail="The parsing engine runs from the command line; it is not exposed by this API yet.",
        ),
    ]


def create_app(
    settings: Settings | None = None,
    *,
    verifier: IdentityVerifier | None = None,
    engine: Engine | None = None,
    run_migrations: bool = False,
    hrms_client_factory=None,
    evaluation_provider_factory=None,
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="KukHire API",
        description=(
            "Applicant tracking for the Kuklabs ecosystem. Sign-in is the shared Kuklabs Account; "
            "workspace access and roles are KukHire's own. AI output is advisory and never decides."
        ),
        version=settings.api_version,
    )
    app.state.settings = settings
    app.state.engine = engine or build_engine(settings.database_url)
    app.state.session_factory = build_session_factory(app.state.engine)
    if verifier is None and settings.auth_base_url:
        verifier = AuthKitRestVerifier(
            settings.auth_base_url,
            product_id=settings.auth_product_id,
            timeout=settings.auth_timeout_seconds,
            cache_seconds=settings.auth_cache_seconds,
        )
    app.state.verifier = verifier
    app.state.hrms_client_factory = hrms_client_factory
    app.state.evaluation_provider_factory = evaluation_provider_factory

    if run_migrations:
        migrations.migrate(app.state.engine)

    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allowed_origins),
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
        )

    @app.exception_handler(httpx.HTTPError)
    async def _upstream(_request, _exc):  # an unhandled upstream error must not echo URLs or keys
        return JSONResponse(status_code=502, content={"detail": {"error": "upstream_error", "message": "Upstream call failed."}})

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(version=settings.api_version)

    @app.get("/ready", tags=["system"])
    def ready():
        """Readiness: the database answers and no migration is pending."""
        try:
            pending = migrations.pending(app.state.engine)
        except Exception:
            return JSONResponse(status_code=503, content={"status": "database_unavailable"})
        if pending:
            return JSONResponse(status_code=503, content={"status": "migrations_pending", "pending": [m.version for m in pending]})
        return {"status": "ready"}

    @app.get("/v1/capabilities", response_model=CapabilitiesResponse, tags=["system"])
    def capabilities() -> CapabilitiesResponse:
        return CapabilitiesResponse(capabilities=_capabilities(settings))

    @app.get("/config.json", tags=["system"], include_in_schema=False)
    def public_config():
        """What the browser UI needs to sign in. No secrets."""
        return {"auth_base_url": settings.auth_base_url or None, "product": settings.auth_product_id, "version": settings.api_version}

    app.include_router(workspace.router)
    app.include_router(ats.router)

    if settings.serve_ui and STATIC_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(STATIC_DIR / "index.html")

    return app


class _LazyApp:
    """`uvicorn app.api:app` without building the app (and its database engine) at import time.

    Importing this module must not need the configured database driver or touch the environment —
    tools and tests import it freely. The real application is created on the first request.
    """

    _instance: FastAPI | None = None

    def _get(self) -> FastAPI:
        if self._instance is None:
            self._instance = create_app()
        return self._instance

    async def __call__(self, scope, receive, send):
        await self._get()(scope, receive, send)

    def __getattr__(self, name):  # e.g. `app.openapi()` from tooling
        return getattr(self._get(), name)


app = _LazyApp()
