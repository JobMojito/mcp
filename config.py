"""Central configuration, loaded from environment variables.

Keeping config in one place makes it easy to override per-environment (local
dev vs. Prefect Horizon deployment) without touching code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Best-effort: load a local .env (gitignored) for development. On Prefect Horizon,
# real values come from deployment environment variables instead.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
STATIC_DIR = REPO_ROOT / "static"


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str | None) -> tuple[str, ...]:
    """Parse a comma-separated env var into a tuple, dropping blanks."""
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    # --- Upstream JobMojito API ---
    api_base_url: str
    openapi_url: str
    supabase_anon_key: str | None  # sent as `apikey` header to Edge Functions
    dev_bearer_token: str | None  # local-only: forwarded when no auth context

    # --- Supabase OAuth ---
    enable_auth: bool
    supabase_project_url: str
    supabase_jwt_algorithm: str
    base_url: str
    site_url: str
    oauth_consent_path: str
    mcp_path: str
    enable_lazy_auth: bool
    oauth_scopes_supported: tuple[str, ...]

    # --- Directory listing / public metadata ---
    openai_apps_challenge_token: str | None
    registry_server_name: str
    marketing_site_url: str
    documentation_url: str
    privacy_policy_url: str
    support_url: str
    server_icon_url: str
    server_icon_mime: str
    server_icon_sizes: tuple[str, ...]

    # --- Result guards ---
    max_tool_result_chars: int

    # --- Documentation ---
    developer_docs_llms_url: str
    developer_docs_base_url: str
    help_docs_base_url: str
    developer_docs_mcp_url: str | None
    developer_docs_mcp_client_id: str | None
    developer_docs_mcp_client_secret: str | None
    developer_docs_mcp_token_url: str | None
    help_docs_mcp_url: str | None
    docs_cache_ttl_minutes: int
    # Featurebase REST API (preferred help-center source when an API key is set)
    featurebase_api_key: str | None
    featurebase_api_base_url: str
    featurebase_api_version: str

    @property
    def mcp_endpoint(self) -> str:
        """The canonical public MCP endpoint (what a user pastes into a client).

        This is also the OAuth *resource identifier* advertised in the protected
        resource metadata, so it must match the URL clients actually connect to,
        character for character.
        """
        return f"{self.base_url}{self.mcp_path}"

    @property
    def base_url_looks_local(self) -> bool:
        return "localhost" in self.base_url or "127.0.0.1" in self.base_url

    @property
    def snapshot_path(self) -> Path:
        return DATA_DIR / "openapi.snapshot.json"

    @property
    def cache_path(self) -> Path:
        # Runtime cache must be writable; containers often have a read-only app
        # dir. Default to a temp dir, overridable via OPENAPI_CACHE_PATH.
        import tempfile

        override = os.environ.get("OPENAPI_CACHE_PATH")
        if override:
            return Path(override)
        return Path(tempfile.gettempdir()) / "jobmojito_openapi.cache.json"

    @property
    def federate_developer_docs(self) -> bool:
        """True when the Mintlify developer-docs MCP should be mounted.

        Only a URL is required: the public `/mcp` endpoint needs no credentials.
        """
        return bool(self.developer_docs_mcp_url)

    @property
    def developer_docs_uses_auth(self) -> bool:
        """True when client-credentials auth should be attached.

        Mintlify's authenticated MCP lives at an `/authed/` path and requires a
        client id/secret; the public `/mcp` endpoint requires neither.
        """
        return bool(
            self.developer_docs_mcp_url
            and "/authed" in self.developer_docs_mcp_url
            and self.developer_docs_mcp_client_id
            and self.developer_docs_mcp_client_secret
        )

    @property
    def developer_docs_token_endpoint(self) -> str | None:
        """OAuth token endpoint for the Mintlify developer-docs MCP."""
        if self.developer_docs_mcp_token_url:
            return self.developer_docs_mcp_token_url
        if self.developer_docs_mcp_url:
            return self.developer_docs_mcp_url.rstrip("/") + "/oauth/token"
        return None


def load_settings() -> Settings:
    return Settings(
        api_base_url=os.environ.get(
            "JOBMOJITO_API_BASE_URL", "https://cool.jobmojito.com/functions/v1"
        ).rstrip("/"),
        openapi_url=os.environ.get(
            "JOBMOJITO_OPENAPI_URL",
            "https://cool.jobmojito.com/functions/v1/openapi",
        ),
        supabase_anon_key=os.environ.get("SUPABASE_ANON_KEY") or None,
        dev_bearer_token=os.environ.get("JOBMOJITO_DEV_BEARER_TOKEN") or None,
        enable_auth=_bool(os.environ.get("ENABLE_AUTH"), True),
        supabase_project_url=os.environ.get(
            "SUPABASE_PROJECT_URL", "https://momsbvnltsydezmoesqt.supabase.co"
        ).rstrip("/"),
        supabase_jwt_algorithm=os.environ.get("SUPABASE_JWT_ALGORITHM", "ES256"),
        base_url=os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/"),
        site_url=os.environ.get("SITE_URL", "https://app.jobmojito.com").rstrip("/"),
        oauth_consent_path=os.environ.get("OAUTH_CONSENT_PATH", "/oauth/consent"),
        mcp_path="/" + os.environ.get("MCP_PATH", "/mcp").strip("/"),
        enable_lazy_auth=_bool(os.environ.get("ENABLE_LAZY_AUTH"), True),
        oauth_scopes_supported=_csv(
            os.environ.get("OAUTH_SCOPES_SUPPORTED", "openid,email")
        ),
        openai_apps_challenge_token=(
            os.environ.get("OPENAI_APPS_CHALLENGE_TOKEN") or None
        ),
        registry_server_name=os.environ.get(
            "REGISTRY_SERVER_NAME", "com.jobmojito/jobmojito"
        ),
        marketing_site_url=os.environ.get(
            "MARKETING_SITE_URL", "https://jobmojito.com"
        ).rstrip("/"),
        documentation_url=os.environ.get(
            "DOCUMENTATION_URL", "https://developer.jobmojito.com/mcp"
        ).rstrip("/"),
        privacy_policy_url=os.environ.get(
            "PRIVACY_POLICY_URL", "https://jobmojito.com/privacy_policy"
        ).rstrip("/"),
        support_url=os.environ.get(
            "SUPPORT_URL", "https://help.jobmojito.com"
        ).rstrip("/"),
        # Directories want a square raster/vector logo (OpenAI: square, 48-4096px,
        # <=5 MiB). A favicon.ico is accepted by MCP clients but is NOT sufficient
        # for a store listing — publish a 512x512 PNG and point SERVER_ICON_URL at it.
        server_icon_url=os.environ.get(
            "SERVER_ICON_URL", "https://jobmojito.com/favicon.png"
        ),
        server_icon_mime=os.environ.get("SERVER_ICON_MIME", "image/png"),
        # Optional "WIDTHxHEIGHT" hints, comma-separated. Omitted when blank —
        # better to say nothing than to advertise the wrong dimensions.
        server_icon_sizes=_csv(os.environ.get("SERVER_ICON_SIZES", "")),
        max_tool_result_chars=int(os.environ.get("MAX_TOOL_RESULT_CHARS", "120000")),
        developer_docs_llms_url=os.environ.get(
            "DEVELOPER_DOCS_LLMS_URL", "https://developer.jobmojito.com/llms.txt"
        ),
        developer_docs_base_url=os.environ.get(
            "DEVELOPER_DOCS_BASE_URL", "https://developer.jobmojito.com"
        ).rstrip("/"),
        help_docs_base_url=os.environ.get(
            "HELP_DOCS_BASE_URL", "https://help.jobmojito.com"
        ).rstrip("/"),
        developer_docs_mcp_url=os.environ.get(
            "DEVELOPER_DOCS_MCP_URL", "https://developer.jobmojito.com/mcp"
        )
        or None,
        developer_docs_mcp_client_id=os.environ.get("DEVELOPER_DOCS_MCP_CLIENT_ID") or None,
        developer_docs_mcp_client_secret=os.environ.get("DEVELOPER_DOCS_MCP_CLIENT_SECRET")
        or None,
        developer_docs_mcp_token_url=os.environ.get("DEVELOPER_DOCS_MCP_TOKEN_URL") or None,
        help_docs_mcp_url=os.environ.get("HELP_DOCS_MCP_URL") or None,
        docs_cache_ttl_minutes=int(os.environ.get("DOCS_CACHE_TTL_MINUTES", "60")),
        featurebase_api_key=os.environ.get("FEATUREBASE_API_KEY") or None,
        featurebase_api_base_url=os.environ.get(
            "FEATUREBASE_API_BASE_URL", "https://do.featurebase.app"
        ).rstrip("/"),
        featurebase_api_version=os.environ.get(
            "FEATUREBASE_API_VERSION", "2026-01-01.nova"
        ),
    )


settings = load_settings()
