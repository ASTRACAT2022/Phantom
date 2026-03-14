from dataclasses import dataclass
from pathlib import Path
import os
import re
import secrets


BASE_DIR = Path(__file__).resolve().parent.parent
LOCAL_FPTN_COMPOSE_PATH = Path("/opt/fptn-server/docker-compose.yml")


@dataclass(frozen=True)
class Settings:
    app_name: str
    database_url: str
    database_path: Path
    fptn_config_dir: Path
    service_name: str
    metrics_url: str
    metrics_insecure_tls: bool
    node_agent_token: str
    billing_api_token: str
    admin_username: str
    admin_password: str
    admin_session_secret: str
    session_cookie_name: str
    session_cookie_secure: bool
    public_base_url: str
    node_agent_grpc_enabled: bool
    node_agent_grpc_host: str
    node_agent_grpc_port: int
    seed_demo: bool
    timezone: str


def _detect_local_fptn_metrics_url() -> str:
    candidates = detect_local_fptn_metrics_urls()
    return candidates[0] if candidates else ""


def _read_local_fptn_compose_body() -> str:
    if not LOCAL_FPTN_COMPOSE_PATH.exists():
        return ""
    try:
        return LOCAL_FPTN_COMPOSE_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def detect_local_fptn_metrics_urls() -> list[str]:
    compose_body = _read_local_fptn_compose_body()
    if not compose_body:
        return []

    proxy_port_match = re.search(
        r'^\s*-\s*"127\.0\.0\.1:(?P<proxy_port>\d+):80/tcp"\s*$',
        compose_body,
        re.MULTILINE,
    )
    port_match = re.search(r'^\s*-\s*"(?P<port>\d+):443/tcp"\s*$', compose_body, re.MULTILINE)
    secret_match = re.search(
        r'^\s*PROMETHEUS_SECRET_ACCESS_KEY:\s*"(?P<secret>[^"]+)"\s*$',
        compose_body,
        re.MULTILINE,
    )
    if not port_match or not secret_match:
        return []

    server_match = re.search(
        r'^\s*SERVER_EXTERNAL_IPS:\s*"(?P<server>[^"]+)"\s*$',
        compose_body,
        re.MULTILINE,
    )
    port = port_match.group("port")
    secret = secret_match.group("secret")
    path = f"/api/v1/metrics/{secret}"
    candidates: list[str] = []
    if proxy_port_match:
        proxy_port = proxy_port_match.group("proxy_port")
        candidates.extend(
            [
                f"http://127.0.0.1:{proxy_port}{path}",
                f"http://localhost:{proxy_port}{path}",
            ]
        )
    candidates.extend(
        [
            f"https://127.0.0.1:{port}{path}",
            f"https://localhost:{port}{path}",
        ]
    )
    if server_match:
        for host in [item.strip() for item in server_match.group("server").split(",") if item.strip()]:
            candidates.append(f"https://{host}:{port}{path}")

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def detect_local_fptn_proxy_domain() -> str:
    compose_body = _read_local_fptn_compose_body()
    if not compose_body:
        return ""

    proxy_match = re.search(
        r'^\s*DEFAULT_PROXY_DOMAIN:\s*"(?P<domain>[^"]+)"\s*$',
        compose_body,
        re.MULTILINE,
    )
    if not proxy_match:
        return ""
    return proxy_match.group("domain").strip()


def load_settings() -> Settings:
    metrics_url = os.getenv("FPTN_PROMETHEUS_METRICS_URL", "").strip()
    if not metrics_url:
        metrics_url = _detect_local_fptn_metrics_url()

    metrics_insecure_tls = os.getenv("FPTN_PROMETHEUS_INSECURE_TLS", "false").lower() == "true"
    if metrics_url.startswith("https://127.0.0.1:") or metrics_url.startswith("https://localhost:"):
        metrics_insecure_tls = True

    return Settings(
        app_name=os.getenv("APP_NAME", "Phantom Control Plane"),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        database_path=Path(
            os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "panel.db"))
        ),
        fptn_config_dir=Path(
            os.getenv("FPTN_CONFIG_DIR", str(BASE_DIR / "fptn-config"))
        ),
        service_name=os.getenv("FPTN_SERVICE_NAME", "ASTRACAT.Network"),
        metrics_url=metrics_url,
        metrics_insecure_tls=metrics_insecure_tls,
        node_agent_token=os.getenv("NODE_CONTROLLER_SHARED_TOKEN", "phantom-node-shared-token"),
        billing_api_token=os.getenv("BILLING_API_TOKEN", "phantom-billing-token"),
        admin_username=os.getenv("ADMIN_USERNAME", "admin").strip() or "admin",
        admin_password=os.getenv("ADMIN_PASSWORD", "admin-change-me"),
        admin_session_secret=os.getenv("ADMIN_SESSION_SECRET", secrets.token_urlsafe(32)),
        session_cookie_name=os.getenv("SESSION_COOKIE_NAME", "phantom_admin_session"),
        session_cookie_secure=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
        public_base_url=os.getenv("PANEL_PUBLIC_BASE_URL", "").strip().rstrip("/"),
        node_agent_grpc_enabled=os.getenv("NODE_AGENT_GRPC_ENABLED", "false").lower() == "true",
        node_agent_grpc_host=os.getenv("NODE_AGENT_GRPC_HOST", "0.0.0.0").strip() or "0.0.0.0",
        node_agent_grpc_port=int(os.getenv("NODE_AGENT_GRPC_PORT", "50061")),
        seed_demo=os.getenv("PHANTOM_SEED_DEMO", "true").lower() == "true",
        timezone=os.getenv("PANEL_TIMEZONE", "Europe/Moscow"),
    )
