import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from .config import BASE_DIR, load_settings
from .node_agent_grpc import start_node_agent_grpc_server
from .service import ControlPlaneService, format_datetime, human_bytes


settings = load_settings()
service = ControlPlaneService(settings)
service.initialize()

app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["datetime"] = format_datetime
templates.env.filters["bytes"] = human_bytes
templates.env.globals["json_dumps"] = json.dumps
app.state.node_agent_grpc_server = None

SESSION_TTL_SECONDS = 60 * 60 * 12
PHANTOM_GITHUB_REPO = "ASTRACAT2022/Phantom"
PHANTOM_GITHUB_REF = "main"
PHANTOM_NODE_CONTROLLER_RAW_BASE = (
    f"https://raw.githubusercontent.com/{PHANTOM_GITHUB_REPO}/{PHANTOM_GITHUB_REF}/node-controller"
)


class BillingLookupPayload(BaseModel):
    username: Optional[str] = None
    billing_subscription_id: Optional[str] = None
    billing_customer_id: Optional[str] = None


class BillingUserUpsertPayload(BaseModel):
    username: str
    bandwidth_mbps: Optional[int] = Field(default=None, ge=0)
    speed_mode: Optional[str] = None
    subscription_days: Optional[int] = Field(default=None, ge=1)
    subscription_expires_at: Optional[str] = None
    is_premium: Optional[bool] = None
    note: Optional[str] = None
    status: Optional[str] = None
    plan_name: Optional[str] = None
    billing_customer_id: Optional[str] = None
    billing_subscription_id: Optional[str] = None


class BillingExtendPayload(BillingLookupPayload):
    days: int = Field(..., ge=1)


class BillingStatusPayload(BillingLookupPayload):
    status: str


class BillingSpeedPayload(BillingLookupPayload):
    speed_mode: Optional[str] = None
    bandwidth_mbps: Optional[int] = Field(default=None, ge=0)


class NodeAgentDeregisterPayload(BaseModel):
    agent_id: str


def nav_items() -> list[dict[str, str]]:
    return [
        {"key": "dashboard", "label": "Dashboard", "href": "/dashboard"},
        {"key": "users", "label": "Users", "href": "/users"},
        {"key": "sessions", "label": "Sessions", "href": "/sessions"},
        {"key": "nodes", "label": "Nodes", "href": "/nodes"},
        {"key": "settings", "label": "Settings", "href": "/settings"},
    ]


def _flash_url(url: str, message: str, level: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["message"] = message
    query["level"] = level
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _session_signature(username: str, expires_at: int) -> str:
    payload = f"{username}:{expires_at}".encode("utf-8")
    return hmac.new(
        settings.admin_session_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def create_session_token(username: str) -> str:
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    signature = _session_signature(username, expires_at)
    payload = f"{username}:{expires_at}:{signature}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8")


def decode_session_token(token: str) -> Optional[str]:
    try:
        payload = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        username, expires_at_raw, signature = payload.split(":", 2)
        expires_at = int(expires_at_raw)
    except Exception:
        return None

    if expires_at < int(time.time()):
        return None

    expected = _session_signature(username, expires_at)
    if not hmac.compare_digest(signature, expected):
        return None
    return username


def current_admin_username(request: Request) -> Optional[str]:
    token = request.cookies.get(settings.session_cookie_name, "")
    if not token:
        return None
    return decode_session_token(token)


def login_redirect(request: Request) -> RedirectResponse:
    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    return RedirectResponse(url=f"/login?next={quote_plus(next_path)}", status_code=303)


def safe_next_path(value: str) -> str:
    candidate = (value or "/dashboard").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/dashboard"
    return candidate


class AdminSessionMiddleware(BaseHTTPMiddleware):
    exempt_prefixes = (
        "/static/",
        "/health",
        "/login",
        "/api/node-agent/",
        "/api/v1/billing/",
    )

    async def dispatch(self, request: Request, call_next):
        if request.url.path != "/" and not any(
            request.url.path.startswith(prefix) for prefix in self.exempt_prefixes
        ):
            if current_admin_username(request) is None:
                return login_redirect(request)

        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


app.add_middleware(AdminSessionMiddleware)


@app.on_event("startup")
async def startup_node_agent_grpc() -> None:
    if not settings.node_agent_grpc_enabled:
        return
    app.state.node_agent_grpc_server = await start_node_agent_grpc_server(settings, service)


@app.on_event("shutdown")
async def shutdown_node_agent_grpc() -> None:
    grpc_server = getattr(app.state, "node_agent_grpc_server", None)
    if grpc_server is None:
        return
    await grpc_server.stop(grace=2)
    app.state.node_agent_grpc_server = None


def redirect_back(
    request: Request,
    message: str,
    level: str = "success",
    fallback: str = "/dashboard",
) -> RedirectResponse:
    referer = request.headers.get("referer", "")
    if referer:
        referer_parts = urlsplit(referer)
        if referer_parts.netloc == request.url.netloc:
            return RedirectResponse(url=_flash_url(referer, message, level), status_code=303)
    return RedirectResponse(url=_flash_url(fallback, message, level), status_code=303)


def panel_base_url(request: Request) -> str:
    if settings.public_base_url:
        return settings.public_base_url
    return str(request.base_url).rstrip("/")


def node_deploy_context(request: Request, node_defaults: dict[str, object]) -> dict[str, object]:
    panel_url = panel_base_url(request)
    panel_host = request.url.hostname or "SERVER_IP"
    default_transport = "grpc" if settings.node_agent_grpc_enabled else "http"
    grpc_target = f"{panel_host}:{settings.node_agent_grpc_port}"
    return {
        "panel_url": panel_url,
        "panel_host": panel_host,
        "node_token": settings.node_agent_token,
        "default_transport": default_transport,
        "grpc_enabled": settings.node_agent_grpc_enabled,
        "grpc_port": settings.node_agent_grpc_port,
        "grpc_target": grpc_target,
        "repo_slug": PHANTOM_GITHUB_REPO,
        "repo_ref": PHANTOM_GITHUB_REF,
        "agent_installer_url": f"{PHANTOM_NODE_CONTROLLER_RAW_BASE}/install-via-github.sh",
        "full_stack_installer_url": f"{PHANTOM_NODE_CONTROLLER_RAW_BASE}/install-fptn-node.sh",
        "default_proxy_domain": node_defaults.get("proxy_domain", "vk.ru"),
        "default_dns_primary": "77.239.113.0",
        "default_dns_secondary": "108.165.164.201",
        "fptn_image": "astracat/fptn-vpn-server:latest",
    }


def render_page(
    request: Request,
    template_name: str,
    page_key: str,
    page_title: str,
    page_description: str,
) -> HTMLResponse:
    context = service.dashboard()
    context.update(
        {
            "request": request,
            "app_name": settings.app_name,
            "admin_username": current_admin_username(request),
            "flash_message": request.query_params.get("message", ""),
            "flash_level": request.query_params.get("level", "success"),
            "page_key": page_key,
            "page_title": page_title,
            "page_description": page_description,
            "nav_items": nav_items(),
        }
    )
    if page_key == "nodes":
        context["node_deploy"] = node_deploy_context(request, context["node_defaults"])
    return templates.TemplateResponse(template_name, context)


def render_login_page(request: Request, error_message: str = "") -> HTMLResponse:
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "page_title": "Admin Login",
            "error_message": error_message,
            "next_path": safe_next_path(request.query_params.get("next", "/dashboard")),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if current_admin_username(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return render_login_page(request)


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_path: str = Form(default="/dashboard"),
) -> Response:
    valid_username = secrets.compare_digest(username.strip(), settings.admin_username)
    valid_password = secrets.compare_digest(password, settings.admin_password)
    if not (valid_username and valid_password):
        return render_login_page(request, error_message="Неверный логин или пароль.")

    response = RedirectResponse(url=safe_next_path(next_path), status_code=303)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=create_session_token(settings.admin_username),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )
    return response


@app.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(settings.session_cookie_name)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> HTMLResponse:
    return render_page(
        request,
        "dashboard.html",
        "dashboard",
        "Operations Dashboard",
        "Обзор VPN-инфраструктуры, live-метрик и того, насколько этим данным можно доверять прямо сейчас.",
    )


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request) -> HTMLResponse:
    return render_page(
        request,
        "users.html",
        "users",
        "Users & Subscriptions",
        "Отдельный раздел для управления пользователями, подписками, ключами доступа и тарифами.",
    )


@app.get("/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request) -> HTMLResponse:
    return render_page(
        request,
        "sessions.html",
        "sessions",
        "Sessions & Active IP",
        "Мониторинг текущих подключений, активных IP и пользовательских сессий без лишнего шума.",
    )


@app.get("/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request) -> HTMLResponse:
    return render_page(
        request,
        "nodes.html",
        "nodes",
        "Infrastructure Nodes",
        "Узлы FPTN, heartbeat от node-controller, нагрузка, порты и состояние edge-инфраструктуры.",
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return render_page(
        request,
        "settings.html",
        "settings",
        "Panel Settings",
        "Настройки панели и node-controller defaults: host, port, tier, region и ручная синхронизация.",
    )


@app.post("/actions/users")
async def create_user(
    request: Request,
    username: str = Form(...),
    bandwidth_mbps: int = Form(...),
    speed_mode: str = Form(default="limited"),
    subscription_days: int = Form(...),
    is_premium: Optional[str] = Form(default=None),
    note: str = Form(default=""),
) -> RedirectResponse:
    try:
        created_username = service.create_user(
            username=username,
            bandwidth_mbps=bandwidth_mbps,
            speed_mode=speed_mode,
            subscription_days=subscription_days,
            is_premium=is_premium == "on",
            note=note,
        )
        return redirect_back(request, f"User '{created_username}' created and synced to FPTN.", fallback="/users")
    except Exception as exc:
        return redirect_back(request, str(exc), level="error", fallback="/users")


@app.post("/actions/users/{user_id}/delete")
async def delete_user(request: Request, user_id: str) -> RedirectResponse:
    service.delete_user(user_id)
    return redirect_back(request, "User deleted from panel and FPTN config.", fallback="/users")


@app.post("/actions/users/{user_id}/status")
async def update_user_status(
    request: Request,
    user_id: str,
    next_status: str = Form(...),
) -> RedirectResponse:
    try:
        service.set_user_status(user_id, next_status)
        return redirect_back(request, f"User status updated to '{next_status}'.", fallback="/users")
    except Exception as exc:
        return redirect_back(request, str(exc), level="error", fallback="/users")


@app.post("/actions/users/{user_id}/subscription")
async def extend_subscription(
    request: Request,
    user_id: str,
    days: int = Form(...),
) -> RedirectResponse:
    service.extend_subscription(user_id, days)
    return redirect_back(request, f"Subscription extended by {days} days.", fallback="/users")


@app.post("/actions/users/{user_id}/rotate-key")
async def rotate_user_key(request: Request, user_id: str) -> RedirectResponse:
    service.rotate_access_key(user_id)
    return redirect_back(request, "Access key rotated and synced to FPTN.", fallback="/users")


@app.post("/actions/users/{user_id}/speed-mode")
async def update_user_speed_mode(
    request: Request,
    user_id: str,
    speed_mode: str = Form(...),
) -> RedirectResponse:
    try:
        service.set_user_speed_mode(user_id, speed_mode)
        return redirect_back(request, "Speed mode updated.", fallback="/users")
    except Exception as exc:
        return redirect_back(request, str(exc), level="error", fallback="/users")


@app.get("/users/{user_id}/config")
async def download_user_config(user_id: str) -> Response:
    bundle = service.get_access_bundle(user_id)
    rotated_at = str(bundle.get("rotated_at", "") or "").replace(":", "").replace("-", "")
    rotated_at = rotated_at.replace("+0000", "Z").replace("+00:00", "Z")
    suffix = f"-{rotated_at}" if rotated_at else ""
    filename = f'{bundle["username"]}{suffix}.fptn'
    return Response(
        content=bundle["token_payload"],
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0, private",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.post("/actions/nodes")
async def create_node(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(...),
    region: str = Form(...),
    tier: str = Form(...),
    md5_fingerprint: str = Form(default=""),
) -> RedirectResponse:
    try:
        service.create_node(name, host, port, region, tier, md5_fingerprint)
        return redirect_back(
            request,
            "Infrastructure node added and FPTN server lists rebuilt.",
            fallback="/nodes",
        )
    except Exception as exc:
        return redirect_back(request, str(exc), level="error", fallback="/nodes")


@app.post("/actions/nodes/{node_id}/delete")
async def delete_node(request: Request, node_id: str) -> RedirectResponse:
    service.delete_node(node_id)
    return redirect_back(request, "Node removed and FPTN server lists rebuilt.", fallback="/nodes")


@app.post("/actions/sync")
async def sync_now(request: Request) -> RedirectResponse:
    service.sync_fptn()
    return redirect_back(request, "Manual FPTN sync completed.", fallback="/settings")


@app.post("/actions/config/node-defaults")
async def update_node_defaults(
    request: Request,
    default_node_host: str = Form(default=""),
    default_node_port: int = Form(...),
    default_node_tier: str = Form(...),
    default_node_region: str = Form(...),
    default_proxy_domain: str = Form(default="vk.ru"),
    node_transport_hint: str = Form(default=""),
) -> RedirectResponse:
    try:
        service.update_node_defaults(
            default_node_host=default_node_host,
            default_node_port=default_node_port,
            default_node_tier=default_node_tier,
            default_node_region=default_node_region,
            default_proxy_domain=default_proxy_domain,
            node_transport_hint=node_transport_hint,
        )
        return redirect_back(
            request,
            "Node defaults updated. Agents can pick up the new port/settings.",
            fallback="/settings",
        )
    except Exception as exc:
        return redirect_back(request, str(exc), level="error", fallback="/settings")


@app.post("/actions/config/speed-policy")
async def update_speed_policy(
    request: Request,
    unlimited_profile_mbps: int = Form(...),
) -> RedirectResponse:
    try:
        service.update_speed_policy(unlimited_profile_mbps)
        return redirect_back(request, "Full speed profile updated.", fallback="/settings")
    except Exception as exc:
        return redirect_back(request, str(exc), level="error", fallback="/settings")


def verify_node_agent(authorization: Optional[str]) -> None:
    if authorization != f"Bearer {settings.node_agent_token}":
        raise HTTPException(status_code=401, detail="Unauthorized node agent.")


def verify_billing_api(authorization: Optional[str]) -> None:
    if authorization != f"Bearer {settings.billing_api_token}":
        raise HTTPException(status_code=401, detail="Unauthorized billing client.")


def require_billing_lookup(payload: BillingLookupPayload) -> None:
    if not any(
        [
            (payload.username or "").strip(),
            (payload.billing_subscription_id or "").strip(),
            (payload.billing_customer_id or "").strip(),
        ]
    ):
        raise HTTPException(
            status_code=400,
            detail="username, billing_subscription_id or billing_customer_id is required.",
        )


@app.get("/api/node-agent/config")
async def node_agent_config(
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_node_agent(authorization)
    return JSONResponse(service.get_node_agent_config())


@app.get("/api/node-agent/fptn-config")
async def node_agent_fptn_config(
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_node_agent(authorization)
    return JSONResponse(service.export_fptn_config_bundle())


@app.post("/actions/users/bulk-unlimited")
async def actions_users_bulk_unlimited(request: Request) -> RedirectResponse:
    verify_admin(request)
    try:
        service.set_all_users_unlimited()
        return flash_redirect(
            request,
            "/settings",
            "Все пользователи успешно переведены на тариф 1000 Mbps.",
        )
    except Exception as exc:
        return flash_redirect(request, "/settings", f"Ошибка: {exc}", level="error")


@app.post("/actions/nodes/{node_id}/delete")
async def delete_node(request: Request, node_id: str) -> RedirectResponse:
    verify_admin(request)
    try:
        if service.delete_node_by_id(node_id):
            return flash_redirect(request, "/nodes", "Node deleted.")
        return flash_redirect(request, "/nodes", "Node not found.", level="error")
    except Exception as exc:
        return flash_redirect(request, "/nodes", str(exc), level="error")


@app.post("/api/node-agent/heartbeat")
async def node_agent_heartbeat(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_node_agent(authorization)

    payload = await request.json()
    try:
        result = service.ingest_node_heartbeat(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.post("/api/node-agent/deregister")
async def node_agent_deregister(
    payload: NodeAgentDeregisterPayload,
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_node_agent(authorization)
    try:
        deleted = service.delete_node_by_agent_id(payload.agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "deleted": deleted, "agent_id": payload.agent_id})


@app.get("/api/v1/billing/users/{username}")
async def billing_user_details(
    username: str,
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_billing_api(authorization)
    try:
        payload = service.get_billing_user(username=username)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "user": payload})


@app.get("/api/v1/billing/subscriptions/check")
async def billing_subscription_check(
    authorization: Optional[str] = Header(default=None),
    username: Optional[str] = None,
    subscription_id: Optional[str] = None,
    customer_id: Optional[str] = None,
) -> JSONResponse:
    verify_billing_api(authorization)
    payload = BillingLookupPayload(
        username=username,
        billing_subscription_id=subscription_id,
        billing_customer_id=customer_id,
    )
    require_billing_lookup(payload)
    try:
        user = service.get_billing_user(
            username=payload.username,
            billing_subscription_id=payload.billing_subscription_id,
            billing_customer_id=payload.billing_customer_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(
        {
            "ok": True,
            "subscription": {
                "username": user["username"],
                "status": user["status"],
                "is_active": user["is_active"],
                "plan_name": user["plan_name"],
                "is_premium": user["is_premium"],
                "speed_mode": user["speed_mode"],
                "bandwidth_mbps": user["bandwidth_mbps"],
                "effective_bandwidth_mbps": user["effective_bandwidth_mbps"],
                "subscription_expires_at": user["subscription_expires_at"],
                "billing_customer_id": user["billing_customer_id"],
                "billing_subscription_id": user["billing_subscription_id"],
            },
        }
    )


@app.get("/api/v1/billing/access-keys/{username}")
async def billing_access_key(
    username: str,
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_billing_api(authorization)
    try:
        payload = service.get_billing_user(username=username)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "username": payload["username"], "access_key": payload["access_key"]})


@app.post("/api/v1/billing/users/upsert")
async def billing_user_upsert(
    payload: BillingUserUpsertPayload,
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_billing_api(authorization)
    try:
        user = service.upsert_billing_user(**payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "user": user})


@app.post("/api/v1/billing/subscriptions/extend")
async def billing_subscription_extend(
    payload: BillingExtendPayload,
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_billing_api(authorization)
    require_billing_lookup(payload)
    try:
        user = service.extend_subscription_by_lookup(
            days=payload.days,
            username=payload.username,
            billing_subscription_id=payload.billing_subscription_id,
            billing_customer_id=payload.billing_customer_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "user": user})


@app.post("/api/v1/billing/subscriptions/status")
async def billing_subscription_status(
    payload: BillingStatusPayload,
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_billing_api(authorization)
    require_billing_lookup(payload)
    try:
        user = service.set_user_status_by_lookup(
            next_status=payload.status,
            username=payload.username,
            billing_subscription_id=payload.billing_subscription_id,
            billing_customer_id=payload.billing_customer_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "user": user})


@app.post("/api/v1/billing/subscriptions/speed")
async def billing_subscription_speed(
    payload: BillingSpeedPayload,
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_billing_api(authorization)
    require_billing_lookup(payload)
    try:
        user = service.update_user_speed_by_lookup(
            speed_mode=payload.speed_mode,
            bandwidth_mbps=payload.bandwidth_mbps,
            username=payload.username,
            billing_subscription_id=payload.billing_subscription_id,
            billing_customer_id=payload.billing_customer_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "user": user})


@app.post("/api/v1/billing/access-keys/rotate")
async def billing_access_key_rotate(
    payload: BillingLookupPayload,
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_billing_api(authorization)
    require_billing_lookup(payload)
    try:
        user = service.rotate_access_key_by_lookup(
            username=payload.username,
            billing_subscription_id=payload.billing_subscription_id,
            billing_customer_id=payload.billing_customer_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "user": user})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": settings.app_name}


@app.get("/openapi.json")
async def openapi_json(request: Request) -> JSONResponse:
    if current_admin_username(request) is None:
        raise HTTPException(status_code=401, detail="Unauthorized.")
    return JSONResponse(get_openapi(title=app.title, version="1.0.0", routes=app.routes))


@app.get("/docs", response_class=HTMLResponse)
async def swagger_docs(request: Request) -> HTMLResponse:
    if current_admin_username(request) is None:
        return login_redirect(request)
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{settings.app_name} Docs")
