from dataclasses import dataclass
from pathlib import Path
import os
import secrets


BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    app_name: str
    database_url: str
    database_path: Path
    fptn_config_dir: Path
    service_name: str
    metrics_url: str
    node_agent_token: str
    billing_api_token: str
    admin_username: str
    admin_password: str
    admin_session_secret: str
    session_cookie_name: str
    session_cookie_secure: bool
    seed_demo: bool
    timezone: str


def load_settings() -> Settings:
    return Settings(
        app_name=os.getenv("APP_NAME", "Phantom Control Plane"),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        database_path=Path(
            os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "panel.db"))
        ),
        fptn_config_dir=Path(
            os.getenv("FPTN_CONFIG_DIR", str(BASE_DIR / "fptn-config"))
        ),
        service_name=os.getenv("FPTN_SERVICE_NAME", "PHANTOM.NET"),
        metrics_url=os.getenv("FPTN_PROMETHEUS_METRICS_URL", "").strip(),
        node_agent_token=os.getenv("NODE_CONTROLLER_SHARED_TOKEN", "phantom-node-shared-token"),
        billing_api_token=os.getenv("BILLING_API_TOKEN", "phantom-billing-token"),
        admin_username=os.getenv("ADMIN_USERNAME", "admin").strip() or "admin",
        admin_password=os.getenv("ADMIN_PASSWORD", "admin-change-me"),
        admin_session_secret=os.getenv("ADMIN_SESSION_SECRET", secrets.token_urlsafe(32)),
        session_cookie_name=os.getenv("SESSION_COOKIE_NAME", "phantom_admin_session"),
        session_cookie_secure=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
        seed_demo=os.getenv("PHANTOM_SEED_DEMO", "true").lower() == "true",
        timezone=os.getenv("PANEL_TIMEZONE", "Europe/Moscow"),
    )
