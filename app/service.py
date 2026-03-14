from __future__ import annotations

import json
import random
import re
import socket
import ssl
import sqlite3
import uuid
from http.client import HTTPSConnection
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping, Optional, Union
from urllib.parse import urlsplit
from urllib.error import URLError
from urllib.request import urlopen

from .config import Settings, detect_local_fptn_metrics_urls, detect_local_fptn_proxy_domain
from .fptn import (
    build_access_link,
    build_access_token,
    generate_password,
    hash_password,
    normalize_username,
    write_fptn_config,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional production dependency
    psycopg = None
    dict_row = None


UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def from_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def human_bytes(value: Union[int, float]) -> str:
    amount = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024
        index += 1
    if index == 0:
        return f"{int(amount)} {units[index]}"
    return f"{amount:.1f} {units[index]}"


def human_rate(value: float) -> str:
    return f"{value:.1f} Mbps"


def human_duration(seconds: Union[int, float, None]) -> str:
    if seconds is None:
        return "n/a"
    total = int(seconds)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def format_datetime(value: Optional[str]) -> str:
    dt = from_iso(value)
    if not dt:
        return "Never"
    return dt.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")


def format_compact_datetime(value: Optional[str]) -> str:
    dt = from_iso(value)
    if not dt:
        return "n/a"
    return dt.astimezone(UTC).strftime("%d %b, %H:%M")


@dataclass
class LiveMetrics:
    connected: bool
    active_sessions: int
    total_incoming_bytes: int
    total_outgoing_bytes: int
    per_user: dict[str, dict[str, int]]
    per_session: dict[str, dict[str, Any]]
    message: str


def _is_local_hostname(hostname: str) -> bool:
    candidate = (hostname or "").strip().lower()
    return candidate in {"127.0.0.1", "localhost", "::1"}


class RowAdapter(Mapping[str, Any]):
    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = dict(data)
        self._keys = list(self._data.keys())

    def __getitem__(self, key: Union[str, int]) -> Any:
        if isinstance(key, int):
            return self._data[self._keys[key]]
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


class CursorResult:
    def __init__(self, cursor: Any) -> None:
        self.cursor = cursor
        description = getattr(cursor, "description", None) or []
        self.columns = [column[0] for column in description]

    def _wrap(self, row: Any) -> Optional[RowAdapter]:
        if row is None:
            return None
        if isinstance(row, RowAdapter):
            return row
        if isinstance(row, Mapping):
            return RowAdapter(row)
        if hasattr(row, "keys"):
            return RowAdapter({key: row[key] for key in row.keys()})
        if self.columns:
            return RowAdapter(dict(zip(self.columns, row)))
        return RowAdapter({})

    def fetchone(self) -> Optional[RowAdapter]:
        return self._wrap(self.cursor.fetchone())

    def fetchall(self) -> list[RowAdapter]:
        return [self._wrap(row) for row in self.cursor.fetchall() if row is not None]

    def __iter__(self) -> Iterator[RowAdapter]:
        for row in self.cursor:
            wrapped = self._wrap(row)
            if wrapped is not None:
                yield wrapped


class DatabaseConnection:
    def __init__(self, backend: str, raw_connection: Any) -> None:
        self.backend = backend
        self.raw_connection = raw_connection

    def _convert_sql(self, sql: str) -> str:
        if self.backend == "postgres":
            return sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> CursorResult:
        sql = self._convert_sql(sql)
        if self.backend == "postgres":
            cursor = self.raw_connection.cursor(row_factory=dict_row)
            cursor.execute(sql, params)
            return CursorResult(cursor)
        cursor = self.raw_connection.execute(sql, params)
        return CursorResult(cursor)

    def executescript(self, sql_script: str) -> None:
        if self.backend == "postgres":
            cursor = self.raw_connection.cursor()
            for statement in sql_script.split(";"):
                statement = statement.strip()
                if statement:
                    cursor.execute(statement)
            cursor.close()
            return
        self.raw_connection.executescript(sql_script)

    def commit(self) -> None:
        self.raw_connection.commit()

    def rollback(self) -> None:
        self.raw_connection.rollback()

    def close(self) -> None:
        self.raw_connection.close()

    def __enter__(self) -> "DatabaseConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            try:
                self.rollback()
            finally:
                self.close()
            return
        try:
            self.commit()
        finally:
            self.close()


class SNIHTTPSConnection(HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: Optional[int] = None,
        *,
        server_hostname: Optional[str] = None,
        timeout: Optional[float] = None,
        context: Optional[ssl.SSLContext] = None,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._server_hostname_override = server_hostname

    def connect(self) -> None:  # pragma: no cover - thin stdlib wrapper
        sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        self.sock = self._context.wrap_socket(
            sock,
            server_hostname=self._server_hostname_override or self.host,
        )


def _fetch_text_response(
    url: str,
    *,
    timeout: float,
    insecure_tls: bool,
    server_name_override: str = "",
) -> str:
    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" or not server_name_override:
        request_kwargs: dict[str, Any] = {"timeout": timeout}
        if parsed_url.scheme == "https" and insecure_tls:
            request_kwargs["context"] = ssl._create_unverified_context()
        with urlopen(url, **request_kwargs) as response:
            return response.read().decode("utf-8")

    context = ssl._create_unverified_context() if insecure_tls else ssl.create_default_context()
    connection = SNIHTTPSConnection(
        parsed_url.hostname or "",
        port=parsed_url.port or 443,
        timeout=timeout,
        context=context,
        server_hostname=server_name_override,
    )
    path = parsed_url.path or "/"
    if parsed_url.query:
        path = f"{path}?{parsed_url.query}"
    connection.request("GET", path, headers={"Host": server_name_override})
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    if response.status >= 400:
        raise URLError(f"{response.status} {response.reason}")
    return body


class ControlPlaneService:
    UNLIMITED_FPTN_BANDWIDTH_MBPS = 2047
    STALE_SESSION_TIMEOUT_MINUTES = 5
    TRAFFIC_SAMPLE_BUCKET_MINUTES = 5

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database_backend = (
            "postgres" if self.settings.database_url.startswith(("postgres://", "postgresql://")) else "sqlite"
        )
        if self.database_backend == "sqlite":
            self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.fptn_config_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> DatabaseConnection:
        if self.database_backend == "postgres":
            if psycopg is None:
                raise RuntimeError(
                    "PostgreSQL support requires psycopg. Install dependencies from requirements.txt."
                )
            connection = psycopg.connect(self.settings.database_url, row_factory=dict_row)
            return DatabaseConnection("postgres", connection)

        connection = sqlite3.connect(self.settings.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return DatabaseConnection("sqlite", connection)

    def initialize(self) -> None:
        with self.connect() as conn:
            self._create_schema(conn)
            self._migrate_schema(conn)
            if self.settings.seed_demo:
                self._seed_demo(conn)
            self._enforce_subscription_state(conn)
        self.sync_fptn()

    def _create_schema(self, conn: DatabaseConnection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_plain TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                bandwidth_mbps INTEGER NOT NULL,
                speed_mode TEXT NOT NULL DEFAULT 'limited',
                is_premium INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                subscription_expires_at TEXT,
                plan_name TEXT NOT NULL DEFAULT 'custom',
                billing_customer_id TEXT NOT NULL DEFAULT '',
                billing_subscription_id TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS access_keys (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                access_link TEXT NOT NULL,
                token_payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                rotated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                agent_id TEXT UNIQUE,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                region TEXT NOT NULL,
                tier TEXT NOT NULL,
                md5_fingerprint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'online',
                source TEXT NOT NULL DEFAULT 'manual',
                last_heartbeat_at TEXT,
                hostname TEXT NOT NULL DEFAULT '',
                uptime_seconds INTEGER,
                load_1 REAL NOT NULL DEFAULT 0,
                load_5 REAL NOT NULL DEFAULT 0,
                load_15 REAL NOT NULL DEFAULT 0,
                memory_total_mb INTEGER NOT NULL DEFAULT 0,
                memory_used_mb INTEGER NOT NULL DEFAULT 0,
                disk_used_percent REAL NOT NULL DEFAULT 0,
                connections_current INTEGER NOT NULL DEFAULT 0,
                fptn_active_sessions INTEGER NOT NULL DEFAULT 0,
                rx_bytes_total INTEGER NOT NULL DEFAULT 0,
                tx_bytes_total INTEGER NOT NULL DEFAULT 0,
                cpu_load REAL NOT NULL DEFAULT 0,
                memory_load REAL NOT NULL DEFAULT 0,
                network_rx_mbps REAL NOT NULL DEFAULT 0,
                network_tx_mbps REAL NOT NULL DEFAULT 0,
                uptime_percent REAL NOT NULL DEFAULT 100,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                vpn_ipv4 TEXT NOT NULL,
                client_version TEXT NOT NULL,
                connected_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                ingress_bytes INTEGER NOT NULL DEFAULT 0,
                egress_bytes INTEGER NOT NULL DEFAULT 0,
                rx_mbps REAL NOT NULL DEFAULT 0,
                tx_mbps REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS traffic_samples (
                id TEXT PRIMARY KEY,
                bucket_time TEXT NOT NULL,
                ingress_mbps REAL NOT NULL DEFAULT 0,
                egress_mbps REAL NOT NULL DEFAULT 0,
                active_users INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.commit()

    def _table_columns(self, conn: DatabaseConnection, table_name: str) -> set[str]:
        if conn.backend == "postgres":
            rows = conn.execute(
                """
                SELECT column_name AS name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = ?
                """,
                (table_name,),
            ).fetchall()
            return {row["name"] for row in rows}
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {row["name"] for row in rows}

    def _migrate_schema(self, conn: DatabaseConnection) -> None:
        node_columns = self._table_columns(conn, "nodes")
        user_columns = self._table_columns(conn, "users")
        migrations = {
            "agent_id": "ALTER TABLE nodes ADD COLUMN agent_id TEXT",
            "source": "ALTER TABLE nodes ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'",
            "last_heartbeat_at": "ALTER TABLE nodes ADD COLUMN last_heartbeat_at TEXT",
            "hostname": "ALTER TABLE nodes ADD COLUMN hostname TEXT NOT NULL DEFAULT ''",
            "uptime_seconds": "ALTER TABLE nodes ADD COLUMN uptime_seconds INTEGER",
            "load_1": "ALTER TABLE nodes ADD COLUMN load_1 REAL NOT NULL DEFAULT 0",
            "load_5": "ALTER TABLE nodes ADD COLUMN load_5 REAL NOT NULL DEFAULT 0",
            "load_15": "ALTER TABLE nodes ADD COLUMN load_15 REAL NOT NULL DEFAULT 0",
            "memory_total_mb": "ALTER TABLE nodes ADD COLUMN memory_total_mb INTEGER NOT NULL DEFAULT 0",
            "memory_used_mb": "ALTER TABLE nodes ADD COLUMN memory_used_mb INTEGER NOT NULL DEFAULT 0",
            "disk_used_percent": "ALTER TABLE nodes ADD COLUMN disk_used_percent REAL NOT NULL DEFAULT 0",
            "connections_current": "ALTER TABLE nodes ADD COLUMN connections_current INTEGER NOT NULL DEFAULT 0",
            "fptn_active_sessions": "ALTER TABLE nodes ADD COLUMN fptn_active_sessions INTEGER NOT NULL DEFAULT 0",
            "rx_bytes_total": "ALTER TABLE nodes ADD COLUMN rx_bytes_total INTEGER NOT NULL DEFAULT 0",
            "tx_bytes_total": "ALTER TABLE nodes ADD COLUMN tx_bytes_total INTEGER NOT NULL DEFAULT 0",
        }
        for column_name, sql in migrations.items():
            if column_name not in node_columns:
                conn.execute(sql)
        if "speed_mode" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN speed_mode TEXT NOT NULL DEFAULT 'limited'"
            )
        if "plan_name" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN plan_name TEXT NOT NULL DEFAULT 'custom'"
            )
        if "billing_customer_id" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN billing_customer_id TEXT NOT NULL DEFAULT ''"
            )
        if "billing_subscription_id" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN billing_subscription_id TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_agent_id ON nodes(agent_id)"
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_billing_subscription_id
            ON users(billing_subscription_id)
            WHERE billing_subscription_id != ''
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_billing_customer_id ON users(billing_customer_id)"
        )
        conn.commit()

    def _seed_demo(self, conn: DatabaseConnection) -> None:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count:
            return

        now = utcnow()
        randomizer = random.Random(42)

        nodes = [
            {
                "id": str(uuid.uuid4()),
                "name": "Edge AMS-01",
                "host": "nl-01.phantom.edge",
                "port": 443,
                "region": "Amsterdam",
                "tier": "public",
                "md5_fingerprint": "02f1ab8392de13c4bcf2201a4f7ea0bb",
                "status": "online",
                "cpu_load": 41.2,
                "memory_load": 58.4,
                "network_rx_mbps": 218.4,
                "network_tx_mbps": 164.1,
                "uptime_percent": 99.99,
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Edge FRA-02",
                "host": "de-02.phantom.edge",
                "port": 443,
                "region": "Frankfurt",
                "tier": "public",
                "md5_fingerprint": "c219c81703cfa21444d2886640e8ca22",
                "status": "online",
                "cpu_load": 63.1,
                "memory_load": 66.0,
                "network_rx_mbps": 184.2,
                "network_tx_mbps": 140.8,
                "uptime_percent": 99.95,
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Core HEL-P",
                "host": "fi-premium.phantom.edge",
                "port": 443,
                "region": "Helsinki",
                "tier": "premium",
                "md5_fingerprint": "ee13a20f431a233a50d4f34199ac0012",
                "status": "online",
                "cpu_load": 55.8,
                "memory_load": 61.7,
                "network_rx_mbps": 272.3,
                "network_tx_mbps": 247.9,
                "uptime_percent": 99.97,
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Shield TR-CZ",
                "host": "tr-censored.phantom.edge",
                "port": 8443,
                "region": "Istanbul",
                "tier": "censored",
                "md5_fingerprint": "8a52ccf614abcc2430d3bc93fe1c6771",
                "status": "warning",
                "cpu_load": 76.2,
                "memory_load": 70.5,
                "network_rx_mbps": 305.4,
                "network_tx_mbps": 288.7,
                "uptime_percent": 99.88,
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Reserve WAW-DR",
                "host": "pl-dr.phantom.edge",
                "port": 443,
                "region": "Warsaw",
                "tier": "public",
                "md5_fingerprint": "1ca440f6f7aa903445ab622eb1d37b0c",
                "status": "offline",
                "cpu_load": 0,
                "memory_load": 0,
                "network_rx_mbps": 0,
                "network_tx_mbps": 0,
                "uptime_percent": 96.41,
            },
        ]

        for node in nodes:
            conn.execute(
                """
                INSERT INTO nodes (
                    id, name, host, port, region, tier, md5_fingerprint, status,
                    cpu_load, memory_load, network_rx_mbps, network_tx_mbps,
                    uptime_percent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node["id"],
                    node["name"],
                    node["host"],
                    node["port"],
                    node["region"],
                    node["tier"],
                    node["md5_fingerprint"],
                    node["status"],
                    node["cpu_load"],
                    node["memory_load"],
                    node["network_rx_mbps"],
                    node["network_tx_mbps"],
                    node["uptime_percent"],
                    to_iso(now),
                ),
            )

        seed_users = [
            ("opsalpha", 120, "unlimited", True, 45, "Core admin team"),
            ("client01", 40, "limited", False, 30, "Starter cohort"),
            ("client02", 25, "limited", False, 14, "Burst traffic"),
            ("streampro", 90, "unlimited", True, 90, "High throughput"),
            ("travelkit", 30, "limited", False, 3, "Expiring soon"),
            ("edgecase", 15, "limited", False, -2, "Expired plan"),
            ("latencyx", 55, "unlimited", True, 60, "Premium gaming"),
            ("auditbox", 20, "limited", False, 12, "Business profile"),
        ]

        public_nodes = [node for node in nodes if node["tier"] == "public"]
        premium_nodes = [node for node in nodes if node["tier"] == "premium"]
        censored_nodes = [node for node in nodes if node["tier"] == "censored"]

        inserted_users: list[dict[str, Any]] = []
        for index, (username, bandwidth, speed_mode, premium, expires_in, note) in enumerate(seed_users):
            user_id = str(uuid.uuid4())
            password_plain = generate_password(10)
            expiry = now + timedelta(days=expires_in) if expires_in else None
            created_at = now - timedelta(days=30 - index * 2)
            status = "expired" if expires_in < 0 else "active"
            conn.execute(
                """
                INSERT INTO users (
                    id, username, password_plain, password_hash, bandwidth_mbps,
                    speed_mode, is_premium, status, subscription_expires_at, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    password_plain,
                    hash_password(password_plain),
                    bandwidth,
                    speed_mode,
                    1 if premium else 0,
                    status,
                    to_iso(expiry),
                    note,
                    to_iso(created_at),
                    to_iso(created_at),
                ),
            )
            token_payload = build_access_token(
                self.settings.service_name,
                username,
                password_plain,
                public_nodes,
                premium_nodes,
                censored_nodes,
                premium,
            )
            conn.execute(
                """
                INSERT INTO access_keys (
                    id, user_id, label, access_link, token_payload, created_at, rotated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    user_id,
                    "Primary key",
                    build_access_link(token_payload),
                    token_payload,
                    to_iso(created_at),
                    to_iso(created_at),
                ),
            )
            inserted_users.append(
                {
                    "id": user_id,
                    "username": username,
                    "is_premium": premium,
                }
            )

        active_node_ids = [node["id"] for node in nodes if node["status"] != "offline"]
        for i in range(11):
            user = inserted_users[i % len(inserted_users)]
            if user["username"] == "edgecase":
                continue
            node_id = active_node_ids[i % len(active_node_ids)]
            connected_at = now - timedelta(hours=randomizer.randint(1, 72))
            last_seen_at = connected_at + timedelta(minutes=randomizer.randint(4, 200))
            active = i % 4 != 0
            ingress_bytes = randomizer.randint(450_000_000, 8_800_000_000)
            egress_bytes = randomizer.randint(350_000_000, 6_200_000_000)
            conn.execute(
                """
                INSERT INTO sessions (
                    id, user_id, node_id, ip_address, vpn_ipv4, client_version,
                    connected_at, last_seen_at, ingress_bytes, egress_bytes,
                    rx_mbps, tx_mbps, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    user["id"],
                    node_id,
                    f"185.24.{20 + i}.{10 + i}",
                    f"10.10.{1 + i // 5}.{10 + i}",
                    f"fptn-client 0.{9 + i}",
                    to_iso(connected_at),
                    to_iso(last_seen_at),
                    ingress_bytes,
                    egress_bytes,
                    round(randomizer.uniform(12, 92), 1) if active else 0,
                    round(randomizer.uniform(8, 71), 1) if active else 0,
                    "active" if active else "terminated",
                ),
            )

        for hour in range(24):
            bucket = now - timedelta(hours=23 - hour)
            conn.execute(
                """
                INSERT INTO traffic_samples (
                    id, bucket_time, ingress_mbps, egress_mbps, active_users
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    to_iso(bucket),
                    round(randomizer.uniform(130, 340), 1),
                    round(randomizer.uniform(90, 250), 1),
                    randomizer.randint(22, 67),
                ),
            )

        self._set_meta(conn, "last_sync_at", to_iso(now))
        conn.commit()

    def _set_meta(self, conn: DatabaseConnection, key: str, value: str) -> None:
        if conn.backend == "postgres":
            conn.execute(
                """
                INSERT INTO meta (key, value)
                VALUES (?, ?)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (key, value),
            )
            return
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )

    def _get_meta(self, conn: DatabaseConnection) -> dict[str, str]:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def _default_node_config(self, meta: Optional[dict[str, str]] = None) -> dict[str, Any]:
        source = meta or {}
        return {
            "host": source.get("default_node_host", ""),
            "port": int(source.get("default_node_port", "8443") or 8443),
            "tier": source.get("default_node_tier", "public"),
            "region": source.get("default_node_region", "Unassigned"),
            "proxy_domain": source.get("default_proxy_domain", "vk.ru"),
            "transport_hint": source.get("node_transport_hint", "Use IP and custom port. Domain/SSL on panel is optional."),
        }

    def _speed_policy(self, meta: Optional[dict[str, str]] = None) -> dict[str, Any]:
        source = meta or {}
        value = int(
            source.get(
                "unlimited_profile_mbps",
                str(self.UNLIMITED_FPTN_BANDWIDTH_MBPS),
            )
            or self.UNLIMITED_FPTN_BANDWIDTH_MBPS
        )
        value = max(1, min(value, self.UNLIMITED_FPTN_BANDWIDTH_MBPS))
        return {"unlimited_profile_mbps": value}

    def _effective_bandwidth_mbps(self, user: dict[str, Any], speed_policy: dict[str, Any]) -> int:
        if user.get("speed_mode") == "unlimited":
            return int(speed_policy["unlimited_profile_mbps"])
        return int(user.get("bandwidth_mbps", 0) or 0)

    def _normalize_speed_mode(self, speed_mode: Optional[str]) -> str:
        normalized = (speed_mode or "limited").strip().lower()
        aliases = {
            "full": "unlimited",
            "full_speed": "unlimited",
            "max": "unlimited",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"limited", "unlimited"}:
            raise ValueError("Unsupported speed mode.")
        return normalized

    def _normalize_user_status(self, status: Optional[str]) -> str:
        normalized = (status or "active").strip().lower()
        aliases = {
            "cancelled": "suspended",
            "canceled": "suspended",
            "paused": "suspended",
            "disabled": "suspended",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"active", "suspended", "expired"}:
            raise ValueError("Unsupported status.")
        return normalized

    def _resolve_user_row(
        self,
        conn: DatabaseConnection,
        *,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        billing_subscription_id: Optional[str] = None,
        billing_customer_id: Optional[str] = None,
    ) -> RowAdapter:
        if user_id:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row:
                return row
        if username:
            normalized_username = normalize_username(username)
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?",
                (normalized_username,),
            ).fetchone()
            if row:
                return row
        if billing_subscription_id:
            row = conn.execute(
                "SELECT * FROM users WHERE billing_subscription_id = ?",
                (billing_subscription_id.strip(),),
            ).fetchone()
            if row:
                return row
        if billing_customer_id:
            row = conn.execute(
                """
                SELECT * FROM users
                WHERE billing_customer_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (billing_customer_id.strip(),),
            ).fetchone()
            if row:
                return row
        raise ValueError("User not found.")

    def _serialize_api_user(
        self,
        conn: DatabaseConnection,
        row: RowAdapter,
    ) -> dict[str, Any]:
        user = dict(row)
        speed_policy = self._speed_policy(self._get_meta(conn))
        access_key = conn.execute(
            """
            SELECT label, access_link, token_payload, created_at, rotated_at
            FROM access_keys
            WHERE user_id = ?
            """,
            (user["id"],),
        ).fetchone()
        session_counts = conn.execute(
            """
            SELECT
                COUNT(*) AS active_sessions,
                COUNT(DISTINCT ip_address) AS active_ips
            FROM sessions
            WHERE user_id = ? AND status = 'active'
            """,
            (user["id"],),
        ).fetchone()
        payload = {
            "id": user["id"],
            "username": user["username"],
            "status": user["status"],
            "is_active": user["status"] == "active",
            "is_premium": bool(user["is_premium"]),
            "bandwidth_mbps": int(user["bandwidth_mbps"]),
            "speed_mode": user.get("speed_mode", "limited"),
            "effective_bandwidth_mbps": self._effective_bandwidth_mbps(user, speed_policy),
            "subscription_expires_at": user["subscription_expires_at"],
            "plan_name": user.get("plan_name", "custom"),
            "billing_customer_id": user.get("billing_customer_id", ""),
            "billing_subscription_id": user.get("billing_subscription_id", ""),
            "note": user.get("note", ""),
            "active_sessions": int(session_counts["active_sessions"] or 0),
            "active_ips": int(session_counts["active_ips"] or 0),
            "created_at": user["created_at"],
            "updated_at": user["updated_at"],
            "access_key": None,
        }
        if access_key:
            payload["access_key"] = {
                "label": access_key["label"],
                "access_link": access_key["access_link"],
                "token_payload": access_key["token_payload"],
                "created_at": access_key["created_at"],
                "rotated_at": access_key["rotated_at"],
            }
        return payload

    def get_node_agent_config(self) -> dict[str, Any]:
        with self.connect() as conn:
            meta = self._get_meta(conn)
        config = self._default_node_config(meta)
        config["agent_transport"] = "grpc" if self.settings.node_agent_grpc_enabled else "http"
        config["grpc_enabled"] = self.settings.node_agent_grpc_enabled
        config["grpc_port"] = self.settings.node_agent_grpc_port
        return config

    def update_node_defaults(
        self,
        default_node_host: str,
        default_node_port: int,
        default_node_tier: str,
        default_node_region: str,
        default_proxy_domain: str,
        node_transport_hint: str,
    ) -> None:
        if default_node_tier not in {"public", "premium", "censored"}:
            raise ValueError("Unsupported default tier.")
        port = int(default_node_port)
        if port < 1 or port > 65535:
            raise ValueError("Port must be between 1 and 65535.")
        proxy_domain = default_proxy_domain.strip() or "vk.ru"

        with self.connect() as conn:
            self._set_meta(conn, "default_node_host", default_node_host.strip())
            self._set_meta(conn, "default_node_port", str(port))
            self._set_meta(conn, "default_node_tier", default_node_tier)
            self._set_meta(conn, "default_node_region", default_node_region.strip() or "Unassigned")
            self._set_meta(conn, "default_proxy_domain", proxy_domain)
            self._set_meta(conn, "node_transport_hint", node_transport_hint.strip())
            conn.commit()

    def update_speed_policy(self, unlimited_profile_mbps: int) -> None:
        value = int(unlimited_profile_mbps)
        if value < 1 or value > self.UNLIMITED_FPTN_BANDWIDTH_MBPS:
            raise ValueError(
                f"Unlimited profile must be between 1 and {self.UNLIMITED_FPTN_BANDWIDTH_MBPS} Mbps."
            )
        with self.connect() as conn:
            self._set_meta(conn, "unlimited_profile_mbps", str(value))
            conn.commit()
        self.sync_fptn()

    def _enforce_subscription_state(self, conn: DatabaseConnection) -> None:
        now = to_iso(utcnow())
        expired_users = conn.execute(
            """
            SELECT id FROM users
            WHERE subscription_expires_at IS NOT NULL
              AND subscription_expires_at < ?
              AND status != 'expired'
            """,
            (now,),
        ).fetchall()
        if not expired_users:
            return

        for row in expired_users:
            conn.execute(
                "UPDATE users SET status = 'expired', updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            conn.execute(
                """
                UPDATE sessions
                SET status = 'terminated', last_seen_at = ?, rx_mbps = 0, tx_mbps = 0
                WHERE user_id = ? AND status = 'active'
                """,
                (now, row["id"]),
            )
        conn.commit()

    def _expire_stale_sessions(self, conn: DatabaseConnection) -> None:
        threshold = to_iso(utcnow() - timedelta(minutes=self.STALE_SESSION_TIMEOUT_MINUTES))
        conn.execute(
            """
            UPDATE sessions
            SET status = 'terminated', rx_mbps = 0, tx_mbps = 0
            WHERE status = 'active' AND last_seen_at < ?
            """,
            (threshold,),
        )
        conn.commit()

    def _record_traffic_sample(
        self,
        ingress_mbps: float,
        egress_mbps: float,
        active_users: int,
    ) -> None:
        bucket_now = utcnow()
        bucket_minute = (bucket_now.minute // self.TRAFFIC_SAMPLE_BUCKET_MINUTES) * self.TRAFFIC_SAMPLE_BUCKET_MINUTES
        bucket = bucket_now.replace(minute=bucket_minute, second=0, microsecond=0)

        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM traffic_samples WHERE bucket_time = ?",
                (to_iso(bucket),),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE traffic_samples
                    SET ingress_mbps = ?, egress_mbps = ?, active_users = ?
                    WHERE id = ?
                    """,
                    (
                        round(float(ingress_mbps), 2),
                        round(float(egress_mbps), 2),
                        int(active_users),
                        existing["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO traffic_samples (
                        id, bucket_time, ingress_mbps, egress_mbps, active_users
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        to_iso(bucket),
                        round(float(ingress_mbps), 2),
                        round(float(egress_mbps), 2),
                        int(active_users),
                    ),
                )
            conn.execute(
                """
                DELETE FROM traffic_samples
                WHERE id NOT IN (
                    SELECT id FROM traffic_samples
                    ORDER BY bucket_time DESC
                    LIMIT 288
                )
                """
            )
            conn.commit()

    def _load_remote_metrics(self) -> LiveMetrics:
        configured_urls = [self.settings.metrics_url] if self.settings.metrics_url else []
        candidate_urls = configured_urls + [
            url for url in detect_local_fptn_metrics_urls() if url not in configured_urls
        ]
        if not candidate_urls:
            return LiveMetrics(False, 0, 0, 0, {}, {}, "FPTN metrics URL is not configured.")

        body = ""
        selected_url = ""
        last_error = "Metrics unavailable."
        local_candidates = set(detect_local_fptn_metrics_urls())
        local_proxy_domain = detect_local_fptn_proxy_domain()
        for candidate_url in candidate_urls:
            try:
                parsed_url = urlsplit(candidate_url)
                use_insecure_tls = parsed_url.scheme == "https" and (
                    self.settings.metrics_insecure_tls
                    or _is_local_hostname(parsed_url.hostname or "")
                    or candidate_url in local_candidates
                )
                server_name_override = (
                    local_proxy_domain
                    if local_proxy_domain and candidate_url in local_candidates
                    else ""
                )
                candidate_body = _fetch_text_response(
                    candidate_url,
                    timeout=3,
                    insecure_tls=use_insecure_tls,
                    server_name_override=server_name_override,
                )
            except URLError as exc:
                last_error = f"Metrics unavailable: {exc.reason}"
                continue
            except Exception as exc:  # pragma: no cover - defensive fallback
                last_error = f"Metrics unavailable: {exc}"
                continue

            if "fptn_active_sessions" not in candidate_body and "fptn_user_" not in candidate_body:
                last_error = f"Metrics endpoint did not return FPTN Prometheus data: {candidate_url}"
                continue

            body = candidate_body
            selected_url = candidate_url
            break

        if not body:
            return LiveMetrics(False, 0, 0, 0, {}, {}, last_error)

        active_sessions = 0
        total_incoming = 0
        total_outgoing = 0
        per_user: dict[str, dict[str, int]] = defaultdict(
            lambda: {"incoming_bytes": 0, "outgoing_bytes": 0}
        )
        per_session: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "session_id": "",
                "username": "unknown",
                "incoming_bytes": 0,
                "outgoing_bytes": 0,
            }
        )

        metric_pattern = re.compile(
            r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[-+0-9.eE]+)$'
        )
        label_pattern = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')

        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = metric_pattern.match(line)
            if not match:
                continue
            name = match.group("name")
            labels = {
                key: value for key, value in label_pattern.findall(match.group("labels") or "")
            }
            try:
                value = float(match.group("value"))
            except ValueError:
                continue

            if name == "fptn_active_sessions":
                active_sessions = int(value)
            elif name == "fptn_user_incoming_traffic_bytes":
                username = labels.get("username", "unknown")
                session_id = labels.get("session_id", "")
                per_user[username]["incoming_bytes"] += int(value)
                if session_id:
                    per_session[session_id]["session_id"] = session_id
                    per_session[session_id]["username"] = username
                    per_session[session_id]["incoming_bytes"] += int(value)
                total_incoming += int(value)
            elif name == "fptn_user_outgoing_traffic_bytes":
                username = labels.get("username", "unknown")
                session_id = labels.get("session_id", "")
                per_user[username]["outgoing_bytes"] += int(value)
                if session_id:
                    per_session[session_id]["session_id"] = session_id
                    per_session[session_id]["username"] = username
                    per_session[session_id]["outgoing_bytes"] += int(value)
                total_outgoing += int(value)

        return LiveMetrics(
            True,
            active_sessions,
            total_incoming,
            total_outgoing,
            dict(per_user),
            dict(per_session),
            f"Live FPTN metrics connected: {selected_url}",
        )

    def _derive_node_status(self, node: dict[str, Any]) -> str:
        last_heartbeat = from_iso(node.get("last_heartbeat_at"))
        if node.get("source") == "agent" and last_heartbeat:
            age_seconds = (utcnow() - last_heartbeat).total_seconds()
            if age_seconds <= 90:
                return "online"
            if age_seconds <= 180:
                return "warning"
            return "offline"
        return node.get("status", "online")

    def _synthetic_live_sessions(
        self,
        live_metrics: LiveMetrics,
        node_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not live_metrics.connected:
            return []

        active_session_count = max(
            int(live_metrics.active_sessions or 0),
            sum(int(node.get("fptn_active_sessions", 0) or 0) for node in node_rows),
        )
        if active_session_count <= 0:
            return []

        preferred_node_name = next(
            (
                node["name"]
                for node in node_rows
                if node.get("status_display") == "online"
            ),
            "Live FPTN",
        )
        synthetic_rows: list[dict[str, Any]] = []
        sorted_sessions = sorted(
            live_metrics.per_session.items(),
            key=lambda item: int(item[0]) if str(item[0]).isdigit() else -1,
            reverse=True,
        )[:active_session_count]
        for session_id, remote in sorted_sessions:
            incoming_bytes = int(remote.get("incoming_bytes", 0) or 0)
            outgoing_bytes = int(remote.get("outgoing_bytes", 0) or 0)
            synthetic_rows.append(
                {
                    "username": remote.get("username", "unknown"),
                    "client_version": f"live metrics / session {session_id}",
                    "ip_address": "n/a",
                    "vpn_ipv4": "n/a",
                    "node_name": preferred_node_name,
                    "rx_mbps": 0,
                    "tx_mbps": 0,
                    "ingress_bytes": incoming_bytes,
                    "egress_bytes": outgoing_bytes,
                    "ingress_human": human_bytes(incoming_bytes),
                    "egress_human": human_bytes(outgoing_bytes),
                    "connected_at_human": "live",
                    "last_seen_human": "live",
                    "status": "active",
                }
            )
        if synthetic_rows:
            return synthetic_rows

        for username, remote in sorted(live_metrics.per_user.items()):
            incoming_bytes = int(remote.get("incoming_bytes", 0) or 0)
            outgoing_bytes = int(remote.get("outgoing_bytes", 0) or 0)
            synthetic_rows.append(
                {
                    "username": username,
                    "client_version": "live metrics",
                    "ip_address": "n/a",
                    "vpn_ipv4": "n/a",
                    "node_name": preferred_node_name,
                    "rx_mbps": 0,
                    "tx_mbps": 0,
                    "ingress_bytes": incoming_bytes,
                    "egress_bytes": outgoing_bytes,
                    "ingress_human": human_bytes(incoming_bytes),
                    "egress_human": human_bytes(outgoing_bytes),
                    "connected_at_human": "live",
                    "last_seen_human": "live",
                    "status": "active",
                }
            )
        if synthetic_rows:
            return synthetic_rows

        for index in range(active_session_count):
            synthetic_rows.append(
                {
                    "username": f"unknown-session-{index + 1}",
                    "client_version": "live session count",
                    "ip_address": "n/a",
                    "vpn_ipv4": "n/a",
                    "node_name": preferred_node_name,
                    "rx_mbps": 0,
                    "tx_mbps": 0,
                    "ingress_bytes": 0,
                    "egress_bytes": 0,
                    "ingress_human": human_bytes(0),
                    "egress_human": human_bytes(0),
                    "connected_at_human": "live",
                    "last_seen_human": "live",
                    "status": "active",
                }
            )
        return synthetic_rows

    def ingest_node_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = (payload.get("agent_id") or "").strip()
        node_payload = payload.get("node") or {}
        system_payload = payload.get("system") or {}
        if not agent_id:
            raise ValueError("agent_id is required.")

        now = utcnow()

        with self.connect() as conn:
            defaults = self._default_node_config(self._get_meta(conn))
            node_defaults = {
                "name": node_payload.get("name", agent_id),
                "host": node_payload.get("host", defaults["host"]),
                "port": int(node_payload.get("port", defaults["port"])),
                "region": node_payload.get("region", defaults["region"]),
                "tier": node_payload.get("tier", defaults["tier"]),
                "md5_fingerprint": node_payload.get("md5_fingerprint", ""),
                "hostname": node_payload.get("hostname", system_payload.get("hostname", "")),
            }
            if not node_defaults["name"]:
                raise ValueError("node.name is required.")
            if not node_defaults["host"]:
                raise ValueError("node.host is required.")

            existing = conn.execute(
                "SELECT * FROM nodes WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            requires_sync = False

            if existing:
                existing_node = dict(existing)
                for field in ("name", "host", "port", "region", "tier", "md5_fingerprint"):
                    if existing_node.get(field) != node_defaults[field]:
                        requires_sync = True
                        break
                conn.execute(
                    """
                    UPDATE nodes
                    SET name = ?, host = ?, port = ?, region = ?, tier = ?, md5_fingerprint = ?,
                        status = 'online', source = 'agent', last_heartbeat_at = ?, hostname = ?,
                        uptime_seconds = ?, load_1 = ?, load_5 = ?, load_15 = ?,
                        cpu_load = ?, memory_load = ?, memory_total_mb = ?, memory_used_mb = ?,
                        disk_used_percent = ?, connections_current = ?, fptn_active_sessions = ?,
                        network_rx_mbps = ?, network_tx_mbps = ?, rx_bytes_total = ?, tx_bytes_total = ?
                    WHERE agent_id = ?
                    """,
                    (
                        node_defaults["name"],
                        node_defaults["host"],
                        node_defaults["port"],
                        node_defaults["region"],
                        node_defaults["tier"],
                        node_defaults["md5_fingerprint"],
                        to_iso(now),
                        node_defaults["hostname"],
                        int(system_payload.get("uptime_seconds", 0) or 0),
                        float(system_payload.get("load1", 0) or 0),
                        float(system_payload.get("load5", 0) or 0),
                        float(system_payload.get("load15", 0) or 0),
                        float(system_payload.get("cpu_percent", 0) or 0),
                        float(system_payload.get("memory_used_percent", 0) or 0),
                        int(system_payload.get("memory_total_mb", 0) or 0),
                        int(system_payload.get("memory_used_mb", 0) or 0),
                        float(system_payload.get("disk_used_percent", 0) or 0),
                        int(system_payload.get("connections_current", 0) or 0),
                        int(system_payload.get("fptn_active_sessions", 0) or 0),
                        float(system_payload.get("network_rx_mbps", 0) or 0),
                        float(system_payload.get("network_tx_mbps", 0) or 0),
                        int(system_payload.get("rx_bytes_total", 0) or 0),
                        int(system_payload.get("tx_bytes_total", 0) or 0),
                        agent_id,
                    ),
                )
                node_id = existing_node["id"]
            else:
                requires_sync = True
                node_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO nodes (
                        id, agent_id, name, host, port, region, tier, md5_fingerprint, status,
                        source, last_heartbeat_at, hostname, uptime_seconds, load_1, load_5, load_15,
                        cpu_load, memory_load, memory_total_mb, memory_used_mb, disk_used_percent,
                        connections_current, fptn_active_sessions, network_rx_mbps, network_tx_mbps,
                        rx_bytes_total, tx_bytes_total, uptime_percent, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        agent_id,
                        node_defaults["name"],
                        node_defaults["host"],
                        node_defaults["port"],
                        node_defaults["region"],
                        node_defaults["tier"],
                        node_defaults["md5_fingerprint"],
                        "online",
                        "agent",
                        to_iso(now),
                        node_defaults["hostname"],
                        int(system_payload.get("uptime_seconds", 0) or 0),
                        float(system_payload.get("load1", 0) or 0),
                        float(system_payload.get("load5", 0) or 0),
                        float(system_payload.get("load15", 0) or 0),
                        float(system_payload.get("cpu_percent", 0) or 0),
                        float(system_payload.get("memory_used_percent", 0) or 0),
                        int(system_payload.get("memory_total_mb", 0) or 0),
                        int(system_payload.get("memory_used_mb", 0) or 0),
                        float(system_payload.get("disk_used_percent", 0) or 0),
                        int(system_payload.get("connections_current", 0) or 0),
                        int(system_payload.get("fptn_active_sessions", 0) or 0),
                        float(system_payload.get("network_rx_mbps", 0) or 0),
                        float(system_payload.get("network_tx_mbps", 0) or 0),
                        int(system_payload.get("rx_bytes_total", 0) or 0),
                        int(system_payload.get("tx_bytes_total", 0) or 0),
                        100,
                        to_iso(now),
                    ),
                )

            if requires_sync:
                self._refresh_all_access_keys(conn)
                speed_policy = self._speed_policy(self._get_meta(conn))
                active_users = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT username, password_hash, bandwidth_mbps, speed_mode, is_premium
                        FROM users
                        WHERE status = 'active'
                        ORDER BY username ASC
                        """
                    )
                ]
                serialized_users = [
                    {
                        **user,
                        "bandwidth_mbps": self._effective_bandwidth_mbps(user, speed_policy),
                    }
                    for user in active_users
                ]
                nodes = [dict(row) for row in conn.execute("SELECT * FROM nodes ORDER BY name ASC")]
                write_fptn_config(
                    self.settings.fptn_config_dir,
                    self.settings.service_name,
                    serialized_users,
                    nodes,
                )
                self._set_meta(conn, "last_sync_at", to_iso(now))

            conn.commit()

        return {"status": "ok", "node_id": node_id, "requires_sync": requires_sync}

    def dashboard(self) -> dict[str, Any]:
        with self.connect() as conn:
            self._enforce_subscription_state(conn)
            self._expire_stale_sessions(conn)
            users = [dict(row) for row in conn.execute("SELECT * FROM users ORDER BY updated_at DESC")]
            nodes = [dict(row) for row in conn.execute("SELECT * FROM nodes ORDER BY name ASC")]
            sessions = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM sessions ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END, last_seen_at DESC"
                )
            ]
            access_keys = [
                dict(row) for row in conn.execute("SELECT * FROM access_keys ORDER BY rotated_at DESC")
            ]
            meta = self._get_meta(conn)
            consistency = self._consistency_report(conn)
            node_defaults = self._default_node_config(meta)
            speed_policy = self._speed_policy(meta)

        live_metrics = self._load_remote_metrics()

        user_keys = {key["user_id"]: key for key in access_keys}
        user_identity_map = {
            user["id"]: user["username"] for user in users
        }
        live_session_counts: dict[str, int] = defaultdict(int)
        for remote_session in live_metrics.per_session.values():
            live_session_counts[remote_session.get("username", "unknown")] += 1
        user_stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "ingress_bytes": 0,
                "egress_bytes": 0,
                "active_sessions": 0,
                "active_ips": set(),
            }
        )
        node_active_users: dict[str, set[str]] = defaultdict(set)

        node_map = {node["id"]: node for node in nodes}
        session_rows: list[dict[str, Any]] = []
        active_session_rows: list[dict[str, Any]] = []
        for session in sessions:
            stats = user_stats[session["user_id"]]
            stats["ingress_bytes"] += int(session["ingress_bytes"])
            stats["egress_bytes"] += int(session["egress_bytes"])
            if session["status"] == "active":
                stats["active_sessions"] += 1
                stats["active_ips"].add(session["ip_address"])
                node_active_users[session["node_id"]].add(session["user_id"])

            session_view = {
                **session,
                "username": user_identity_map.get(session["user_id"], "unknown"),
                "node_name": node_map.get(session["node_id"], {}).get("name", "Unknown"),
                "connected_at_human": format_compact_datetime(session["connected_at"]),
                "last_seen_human": format_compact_datetime(session["last_seen_at"]),
                "ingress_human": human_bytes(session["ingress_bytes"]),
                "egress_human": human_bytes(session["egress_bytes"]),
            }
            session_rows.append(session_view)
            if session["status"] == "active":
                active_session_rows.append(session_view)

        expiring_soon = 0
        user_rows: list[dict[str, Any]] = []
        for user in users:
            stats = user_stats[user["id"]]
            remote = live_metrics.per_user.get(user["username"], {})
            ingress_bytes = remote.get("incoming_bytes", stats["ingress_bytes"])
            egress_bytes = remote.get("outgoing_bytes", stats["egress_bytes"])
            expires_at = from_iso(user["subscription_expires_at"])
            if expires_at and expires_at > utcnow() and expires_at < utcnow() + timedelta(days=7):
                expiring_soon += 1
            user_rows.append(
                {
                    **user,
                    "is_premium": bool(user["is_premium"]),
                    "speed_mode": user.get("speed_mode", "limited"),
                    "effective_bandwidth_mbps": self._effective_bandwidth_mbps(user, speed_policy),
                    "speed_label": (
                        "Full speed"
                        if user.get("speed_mode") == "unlimited"
                        else f'{int(user["bandwidth_mbps"])} Mbps'
                    ),
                    "active_sessions": max(
                        stats["active_sessions"],
                        live_session_counts.get(user["username"], 0),
                    ),
                    "active_ips": len(stats["active_ips"]),
                    "ingress_bytes": ingress_bytes,
                    "egress_bytes": egress_bytes,
                    "ingress_human": human_bytes(ingress_bytes),
                    "egress_human": human_bytes(egress_bytes),
                    "expires_human": format_datetime(user["subscription_expires_at"]),
                    "created_human": format_compact_datetime(user["created_at"]),
                    "key": user_keys.get(user["id"]),
                }
            )

        user_rows.sort(
            key=lambda item: (
                0 if item["status"] == "active" else 1,
                -item["active_sessions"],
                item["username"],
            )
        )

        node_rows: list[dict[str, Any]] = []
        for node in nodes:
            derived_status = self._derive_node_status(node)
            node_rows.append(
                {
                    **node,
                    "status_display": derived_status,
                    "active_users": len(node_active_users.get(node["id"], set())),
                    "last_heartbeat_human": format_compact_datetime(node.get("last_heartbeat_at")),
                    "uptime_human": human_duration(node.get("uptime_seconds")),
                    "memory_human": (
                        f'{int(node.get("memory_used_mb", 0))} / {int(node.get("memory_total_mb", 0))} MB'
                        if int(node.get("memory_total_mb", 0) or 0) > 0
                        else "n/a"
                    ),
                }
            )

        synthetic_sessions = self._synthetic_live_sessions(live_metrics, node_rows)
        if live_metrics.connected and synthetic_sessions:
            display_sessions = synthetic_sessions
        else:
            display_sessions = active_session_rows

        total_ingress = sum(user["ingress_bytes"] for user in user_rows)
        total_egress = sum(user["egress_bytes"] for user in user_rows)
        active_sessions = sum(1 for session in sessions if session["status"] == "active")
        active_ips = len(
            {
                session["ip_address"]
                for session in sessions
                if session["status"] == "active"
            }
        )
        total_rx = sum(float(session["rx_mbps"]) for session in sessions if session["status"] == "active")
        total_tx = sum(float(session["tx_mbps"]) for session in sessions if session["status"] == "active")
        if total_rx <= 0:
            total_rx = sum(float(node.get("network_rx_mbps", 0) or 0) for node in node_rows)
        if total_tx <= 0:
            total_tx = sum(float(node.get("network_tx_mbps", 0) or 0) for node in node_rows)

        active_user_count = len(
            {
                session["username"]
                for session in display_sessions
                if session.get("username") and not str(session["username"]).startswith("unknown-session-")
            }
        )
        self._record_traffic_sample(total_rx, total_tx, active_user_count)
        with self.connect() as conn:
            traffic_samples = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM traffic_samples ORDER BY bucket_time DESC LIMIT 24"
                )
            ]
        traffic_samples.reverse()

        online_nodes = sum(1 for node in node_rows if node["status_display"] != "offline")
        expired_users = sum(1 for user in user_rows if user["status"] == "expired")
        agent_nodes_total = sum(1 for node in node_rows if node["source"] == "agent")
        agent_nodes_online = sum(
            1 for node in node_rows if node["source"] == "agent" and node["status_display"] == "online"
        )
        manual_nodes_total = sum(1 for node in node_rows if node["source"] != "agent")

        alerts: list[str] = []
        if expiring_soon:
            alerts.append(f"{expiring_soon} subscriptions expire within 7 days.")
        if expired_users:
            alerts.append(f"{expired_users} users are already expired and disconnected.")
        offline_nodes = [node["name"] for node in node_rows if node["status_display"] == "offline"]
        if offline_nodes:
            alerts.append(f"Offline edge detected: {', '.join(offline_nodes)}.")
        loaded_nodes = [node["name"] for node in node_rows if node["cpu_load"] >= 75]
        if loaded_nodes:
            alerts.append(f"High CPU load on: {', '.join(loaded_nodes)}.")
        if not consistency["ok"]:
            alerts.append(
                "Runtime consistency issue detected between active database, exported users.list or access keys. Open Settings for details."
            )
        if not alerts:
            alerts.append("Infrastructure looks stable. No urgent incidents detected.")

        data_accuracy_notes: list[str] = []
        if live_metrics.connected:
            data_accuracy_notes.append(
                "Суммарный трафик пользователей и счётчик сессий приходят из live-метрик FPTN."
            )
            if live_metrics.per_session:
                data_accuracy_notes.append(
                    "Список live-сессий строится по session_id и username из Prometheus-метрик FPTN."
                )
            elif live_metrics.active_sessions:
                data_accuracy_notes.append(
                    "Если FPTN отдал только общий счётчик сессий без session traffic, панель покажет fallback по live session count."
                )
        else:
            data_accuracy_notes.append(
                "Трафик и часть counters считаются из данных панели. Без live-метрик FPTN эти значения приблизительные."
            )
        if agent_nodes_total:
            data_accuracy_notes.append(
                f"{agent_nodes_online}/{agent_nodes_total} узлов уже отдают live heartbeat через node-controller."
            )
        else:
            data_accuracy_notes.append(
                "Карточки узлов сейчас используют сохранённые/manual значения. Для точного uptime и load нужно поставить node-controller на реальные ноды."
            )
        if self.settings.seed_demo and not live_metrics.connected and agent_nodes_total == 0:
            data_accuracy_notes.append(
                "Включён demo-режим, поэтому часть значений является тестовыми данными для интерфейса."
            )

        overview = {
            "total_users": len(user_rows),
            "active_subscriptions": sum(1 for user in user_rows if user["status"] == "active"),
            "expiring_soon": expiring_soon,
            "active_sessions": live_metrics.active_sessions
            or sum(int(node.get("fptn_active_sessions", 0) or 0) for node in node_rows)
            or active_sessions,
            "active_ips": active_ips
            or sum(int(node.get("connections_current", 0) or 0) for node in node_rows),
            "total_ingress": human_bytes(
                live_metrics.total_incoming_bytes or total_ingress
            ),
            "total_egress": human_bytes(
                live_metrics.total_outgoing_bytes or total_egress
            ),
            "download_rate": human_rate(total_rx),
            "upload_rate": human_rate(total_tx),
            "online_nodes": online_nodes,
            "agent_nodes_online": agent_nodes_online,
        }

        if not traffic_samples:
            traffic_samples = [
                {
                    "bucket_time": to_iso(utcnow()),
                    "ingress_mbps": round(float(total_rx), 2),
                    "egress_mbps": round(float(total_tx), 2),
                    "active_users": int(active_user_count),
                }
            ]

        traffic_chart = {
            "labels": [format_compact_datetime(item["bucket_time"]) for item in traffic_samples],
            "ingress": [item["ingress_mbps"] for item in traffic_samples],
            "egress": [item["egress_mbps"] for item in traffic_samples],
            "active_users": [item["active_users"] for item in traffic_samples],
        }

        expiring_users = sorted(
            [user for user in user_rows if user["status"] == "active"],
            key=lambda user: user["subscription_expires_at"] or "9999-12-31T00:00:00+00:00",
        )[:5]
        hot_nodes = sorted(
            node_rows,
            key=lambda node: (node["status_display"] == "offline", -float(node["cpu_load"])),
        )[:4]

        return {
            "overview": overview,
            "users": user_rows,
            "sessions": display_sessions,
            "nodes": node_rows,
            "alerts": alerts,
            "data_accuracy_notes": data_accuracy_notes,
            "data_sources": {
                "traffic": "live_fptn" if live_metrics.connected else "panel_storage",
                "nodes": "node_agent" if agent_nodes_total else "manual",
                "mode": "demo" if self.settings.seed_demo else "operational",
                "agent_nodes_total": agent_nodes_total,
                "manual_nodes_total": manual_nodes_total,
            },
            "expiring_users": expiring_users,
            "recent_sessions": display_sessions[:10],
            "hot_nodes": hot_nodes,
            "traffic_chart": traffic_chart,
            "traffic_chart_json": json.dumps(traffic_chart),
            "last_sync_at": format_datetime(meta.get("last_sync_at")),
            "metrics_status": live_metrics,
            "config_dir": str(self.settings.fptn_config_dir),
            "consistency": consistency,
            "node_defaults": node_defaults,
            "speed_policy": speed_policy,
        }

    def _current_nodes(self, conn: DatabaseConnection) -> tuple[list[dict], list[dict], list[dict]]:
        rows = [dict(row) for row in conn.execute("SELECT * FROM nodes")]
        eligible_rows = []
        for row in rows:
            host = str(row.get("host", "") or "").strip()
            port = int(row.get("port", 0) or 0)
            md5_fingerprint = str(row.get("md5_fingerprint", "") or "").strip().lower()
            if not host or port < 1 or port > 65535:
                continue
            if not md5_fingerprint:
                continue
            if row.get("source") == "agent" and self._derive_node_status(row) == "offline":
                continue
            eligible_rows.append(row)
        public_nodes = [row for row in eligible_rows if row["tier"] == "public"]
        premium_nodes = [row for row in eligible_rows if row["tier"] == "premium"]
        censored_nodes = [row for row in eligible_rows if row["tier"] == "censored"]
        return public_nodes, premium_nodes, censored_nodes

    def _repair_user_hashes(self, conn: DatabaseConnection) -> bool:
        updated = False
        rows = conn.execute(
            "SELECT id, password_plain, password_hash FROM users"
        ).fetchall()
        for row in rows:
            expected_hash = hash_password(row["password_plain"])
            if row["password_hash"] != expected_hash:
                conn.execute(
                    "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                    (expected_hash, to_iso(utcnow()), row["id"]),
                )
                updated = True
        return updated

    def _users_list_hash_for_username(self, username: str) -> str:
        users_list_path = self.settings.fptn_config_dir / "users.list"
        if not users_list_path.exists():
            return ""
        for raw_line in users_list_path.read_text(encoding="utf-8").splitlines():
            parts = raw_line.strip().split()
            if len(parts) >= 2 and parts[0] == username:
                return parts[1]
        return ""

    def _validate_token_payload(self, username: str, token_payload: str) -> tuple[bool, str]:
        try:
            payload = json.loads(token_payload)
        except json.JSONDecodeError as exc:
            return False, f"invalid token payload json: {exc}"

        token_username = str(payload.get("username", "") or "")
        token_password = str(payload.get("password", "") or "")
        if token_username != username:
            return False, "token username mismatch"
        if not token_password:
            return False, "token password is empty"

        exported_hash = self._users_list_hash_for_username(username)
        if not exported_hash:
            return False, f"{username} is missing from users.list"
        if hash_password(token_password) != exported_hash:
            return False, "token password does not match users.list hash"

        all_servers = list(payload.get("servers", [])) + list(payload.get("censored_zone_servers", []))
        if not all_servers:
            return False, "token payload has no servers"
        for server in all_servers:
            host = str(server.get("host", "") or "").strip()
            port = int(server.get("port", 0) or 0)
            fingerprint = str(server.get("md5_fingerprint", "") or "").strip().lower()
            if not host or port < 1 or port > 65535:
                return False, "token payload contains server with invalid host/port"
            if not fingerprint:
                return False, "token payload contains server without fingerprint"
        return True, "ok"

    def _read_exported_users(self) -> dict[str, dict[str, Any]]:
        users_list_path = self.settings.fptn_config_dir / "users.list"
        exported: dict[str, dict[str, Any]] = {}
        if not users_list_path.exists():
            return exported
        for raw_line in users_list_path.read_text(encoding="utf-8").splitlines():
            parts = raw_line.strip().split()
            if len(parts) < 4:
                continue
            username, password_hash, bandwidth_mbps, premium_flag = parts[:4]
            exported[username] = {
                "password_hash": password_hash,
                "bandwidth_mbps": int(bandwidth_mbps),
                "is_premium": premium_flag == "1",
            }
        return exported

    def _read_exported_nodes(self) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for file_name, tier in (
            ("servers.json", "public"),
            ("premium_servers.json", "premium"),
            ("servers_censored_zone.json", "censored"),
        ):
            path = self.settings.fptn_config_dir / file_name
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for item in payload:
                nodes.append(
                    {
                        "name": str(item.get("name", "") or ""),
                        "host": str(item.get("host", "") or ""),
                        "port": int(item.get("port", 0) or 0),
                        "tier": tier,
                        "md5_fingerprint": str(item.get("md5_fingerprint", "") or "").lower(),
                    }
                )
        return nodes

    def _consistency_report(self, conn: DatabaseConnection) -> dict[str, Any]:
        active_users = [
            dict(row)
            for row in conn.execute(
                """
                SELECT username, password_hash, is_premium, bandwidth_mbps
                FROM users
                WHERE status = 'active'
                ORDER BY username ASC
                """
            )
        ]
        exported_users = self._read_exported_users()
        active_usernames = [row["username"] for row in active_users]
        exported_usernames = sorted(exported_users.keys())

        missing_in_export = sorted(set(active_usernames) - set(exported_usernames))
        orphaned_in_export = sorted(set(exported_usernames) - set(active_usernames))
        hash_mismatches = sorted(
            row["username"]
            for row in active_users
            if exported_users.get(row["username"], {}).get("password_hash") != row["password_hash"]
        )

        access_key_rows = conn.execute(
            """
            SELECT users.username, access_keys.token_payload
            FROM access_keys
            JOIN users ON users.id = access_keys.user_id
            WHERE users.status = 'active'
            ORDER BY users.username ASC
            """
        ).fetchall()
        invalid_access_keys: list[str] = []
        for row in access_key_rows:
            valid, _ = self._validate_token_payload(row["username"], row["token_payload"])
            if not valid:
                invalid_access_keys.append(row["username"])

        exported_nodes = self._read_exported_nodes()
        exported_node_names = sorted(node["name"] for node in exported_nodes if node["name"])

        database_target = self.settings.database_url if self.database_backend == "postgres" else str(self.settings.database_path)

        ok = not (missing_in_export or orphaned_in_export or hash_mismatches or invalid_access_keys)
        return {
            "ok": ok,
            "database_backend": self.database_backend,
            "database_target": database_target,
            "active_users_count": len(active_users),
            "exported_users_count": len(exported_users),
            "active_usernames": active_usernames,
            "exported_usernames": exported_usernames,
            "missing_in_export": missing_in_export,
            "orphaned_in_export": orphaned_in_export,
            "hash_mismatches": hash_mismatches,
            "invalid_access_keys": invalid_access_keys,
            "exported_nodes_count": len(exported_nodes),
            "exported_node_names": exported_node_names,
        }

    def _upsert_access_key(
        self,
        conn: DatabaseConnection,
        user_id: str,
        username: str,
        password_plain: str,
        is_premium: bool,
    ) -> None:
        public_nodes, premium_nodes, censored_nodes = self._current_nodes(conn)
        token_payload = build_access_token(
            self.settings.service_name,
            username,
            password_plain,
            public_nodes,
            premium_nodes,
            censored_nodes,
            is_premium,
        )
        now = to_iso(utcnow())
        existing = conn.execute(
            "SELECT id, created_at FROM access_keys WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE access_keys
                SET access_link = ?, token_payload = ?, rotated_at = ?, label = ?
                WHERE user_id = ?
                """,
                (
                    build_access_link(token_payload),
                    token_payload,
                    now,
                    "Primary key",
                    user_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO access_keys (
                    id, user_id, label, access_link, token_payload, created_at, rotated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    user_id,
                    "Primary key",
                    build_access_link(token_payload),
                    token_payload,
                    now,
                    now,
                ),
            )

    def _refresh_all_access_keys(self, conn: DatabaseConnection) -> None:
        users = conn.execute(
            "SELECT id, username, password_plain, is_premium FROM users"
        ).fetchall()
        for user in users:
            self._upsert_access_key(
                conn,
                user["id"],
                user["username"],
                user["password_plain"],
                bool(user["is_premium"]),
            )

    def create_user(
        self,
        username: str,
        bandwidth_mbps: int,
        speed_mode: str,
        subscription_days: int,
        is_premium: bool,
        note: str,
    ) -> str:
        username = normalize_username(username)
        if not username:
            username = f"user{random.randint(1000, 9999)}"

        password_plain = generate_password(10)
        now = utcnow()
        expires_at = now + timedelta(days=max(subscription_days, 1))
        user_id = str(uuid.uuid4())
        speed_mode = self._normalize_speed_mode(speed_mode)
        bandwidth_mbps = max(1, int(bandwidth_mbps))

        with self.connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,)
            ).fetchone()
            if existing:
                raise ValueError(f"Username '{username}' already exists.")

            conn.execute(
                """
                INSERT INTO users (
                    id, username, password_plain, password_hash, bandwidth_mbps,
                    speed_mode, is_premium, status, subscription_expires_at, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    password_plain,
                    hash_password(password_plain),
                    bandwidth_mbps,
                    speed_mode,
                    1 if is_premium else 0,
                    "active",
                    to_iso(expires_at),
                    note.strip(),
                    to_iso(now),
                    to_iso(now),
                ),
            )
            self._upsert_access_key(conn, user_id, username, password_plain, is_premium)
            conn.commit()

        self.sync_fptn()
        return username

    def delete_user(self, user_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
        self.sync_fptn()

    def set_user_status(self, user_id: str, next_status: str) -> None:
        next_status = self._normalize_user_status(next_status)
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
                (next_status, to_iso(utcnow()), user_id),
            )
            if next_status != "active":
                conn.execute(
                    """
                    UPDATE sessions
                    SET status = 'terminated', last_seen_at = ?, rx_mbps = 0, tx_mbps = 0
                    WHERE user_id = ? AND status = 'active'
                    """,
                    (to_iso(utcnow()), user_id),
                )
            conn.commit()
        self.sync_fptn()

    def extend_subscription(self, user_id: str, days: int) -> None:
        now = utcnow()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT subscription_expires_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                raise ValueError("User not found.")
            current_expires = from_iso(row["subscription_expires_at"]) or now
            base = current_expires if current_expires > now else now
            expires_at = base + timedelta(days=max(days, 1))
            conn.execute(
                """
                UPDATE users
                SET subscription_expires_at = ?, status = 'active', updated_at = ?
                WHERE id = ?
                """,
                (to_iso(expires_at), to_iso(now), user_id),
            )
            conn.commit()
        self.sync_fptn()

    def rotate_access_key(self, user_id: str) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT username, is_premium FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                raise ValueError("User not found.")
            password_plain = generate_password(10)
            conn.execute(
                """
                UPDATE users
                SET password_plain = ?, password_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    password_plain,
                    hash_password(password_plain),
                    to_iso(utcnow()),
                    user_id,
                ),
            )
            self._upsert_access_key(
                conn,
                user_id,
                row["username"],
                password_plain,
                bool(row["is_premium"]),
            )
            conn.commit()
        self.sync_fptn()

    def set_user_speed_mode(self, user_id: str, speed_mode: str) -> None:
        speed_mode = self._normalize_speed_mode(speed_mode)
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET speed_mode = ?, updated_at = ? WHERE id = ?",
                (speed_mode, to_iso(utcnow()), user_id),
            )
            conn.commit()
        self.sync_fptn()

    def get_billing_user(
        self,
        *,
        username: Optional[str] = None,
        billing_subscription_id: Optional[str] = None,
        billing_customer_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            self._enforce_subscription_state(conn)
            row = self._resolve_user_row(
                conn,
                username=username,
                billing_subscription_id=billing_subscription_id,
                billing_customer_id=billing_customer_id,
            )
            return self._serialize_api_user(conn, row)

    def upsert_billing_user(
        self,
        *,
        username: str,
        bandwidth_mbps: Optional[int] = None,
        speed_mode: Optional[str] = None,
        subscription_days: Optional[int] = None,
        subscription_expires_at: Optional[str] = None,
        is_premium: Optional[bool] = None,
        note: Optional[str] = None,
        status: Optional[str] = None,
        plan_name: Optional[str] = None,
        billing_customer_id: Optional[str] = None,
        billing_subscription_id: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_username = normalize_username(username)
        if not normalized_username:
            raise ValueError("username is required.")

        normalized_speed_mode = (
            self._normalize_speed_mode(speed_mode) if speed_mode is not None else None
        )
        normalized_status = (
            self._normalize_user_status(status) if status is not None else None
        )
        parsed_expires_at = None
        if subscription_expires_at is not None:
            parsed_expires_at = from_iso(subscription_expires_at)
            if parsed_expires_at is None:
                raise ValueError("subscription_expires_at must be a valid ISO-8601 datetime.")

        now = utcnow()
        if subscription_days is not None:
            subscription_days = max(1, int(subscription_days))

        with self.connect() as conn:
            existing = None
            lookup_subscription_id = (billing_subscription_id or "").strip()
            lookup_customer_id = (billing_customer_id or "").strip()
            try:
                existing = self._resolve_user_row(
                    conn,
                    username=normalized_username,
                    billing_subscription_id=lookup_subscription_id or None,
                    billing_customer_id=lookup_customer_id or None,
                )
            except ValueError:
                existing = None

            if existing:
                current = dict(existing)
                conflict = conn.execute(
                    "SELECT id FROM users WHERE username = ? AND id != ?",
                    (normalized_username, current["id"]),
                ).fetchone()
                if conflict:
                    raise ValueError(f"Username '{normalized_username}' already exists.")

                current_expires_at = from_iso(current["subscription_expires_at"])
                next_expires_at = current["subscription_expires_at"]
                if parsed_expires_at is not None:
                    next_expires_at = to_iso(parsed_expires_at)
                elif subscription_days is not None:
                    base = current_expires_at if current_expires_at and current_expires_at > now else now
                    next_expires_at = to_iso(base + timedelta(days=subscription_days))

                next_status = normalized_status or current["status"]
                next_speed_mode = normalized_speed_mode or current.get("speed_mode", "limited")
                next_is_premium = bool(is_premium) if is_premium is not None else bool(current["is_premium"])
                next_bandwidth = (
                    max(1, int(bandwidth_mbps))
                    if bandwidth_mbps is not None
                    else int(current["bandwidth_mbps"])
                )
                next_note = note.strip() if note is not None else current.get("note", "")
                next_plan_name = (
                    plan_name.strip() if plan_name is not None else current.get("plan_name", "custom")
                ) or "custom"
                next_customer_id = (
                    billing_customer_id.strip()
                    if billing_customer_id is not None
                    else current.get("billing_customer_id", "")
                )
                next_subscription_id = (
                    billing_subscription_id.strip()
                    if billing_subscription_id is not None
                    else current.get("billing_subscription_id", "")
                )
                if next_subscription_id:
                    subscription_conflict = conn.execute(
                        """
                        SELECT id FROM users
                        WHERE billing_subscription_id = ? AND id != ?
                        """,
                        (next_subscription_id, current["id"]),
                    ).fetchone()
                    if subscription_conflict:
                        raise ValueError(
                            f"billing_subscription_id '{next_subscription_id}' already exists."
                        )

                conn.execute(
                    """
                    UPDATE users
                    SET username = ?, bandwidth_mbps = ?, speed_mode = ?, is_premium = ?,
                        status = ?, subscription_expires_at = ?, plan_name = ?,
                        billing_customer_id = ?, billing_subscription_id = ?, note = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        normalized_username,
                        next_bandwidth,
                        next_speed_mode,
                        1 if next_is_premium else 0,
                        next_status,
                        next_expires_at,
                        next_plan_name,
                        next_customer_id,
                        next_subscription_id,
                        next_note,
                        to_iso(now),
                        current["id"],
                    ),
                )
                if next_status != "active":
                    conn.execute(
                        """
                        UPDATE sessions
                        SET status = 'terminated', last_seen_at = ?, rx_mbps = 0, tx_mbps = 0
                        WHERE user_id = ? AND status = 'active'
                        """,
                        (to_iso(now), current["id"]),
                    )
                self._upsert_access_key(
                    conn,
                    current["id"],
                    normalized_username,
                    current["password_plain"],
                    next_is_premium,
                )
                row_id = current["id"]
            else:
                password_plain = generate_password(10)
                expires_at = parsed_expires_at or (now + timedelta(days=subscription_days or 30))
                row_id = str(uuid.uuid4())
                if billing_subscription_id:
                    subscription_conflict = conn.execute(
                        "SELECT 1 FROM users WHERE billing_subscription_id = ?",
                        (billing_subscription_id.strip(),),
                    ).fetchone()
                    if subscription_conflict:
                        raise ValueError(
                            f"billing_subscription_id '{billing_subscription_id.strip()}' already exists."
                        )
                conn.execute(
                    """
                    INSERT INTO users (
                        id, username, password_plain, password_hash, bandwidth_mbps,
                        speed_mode, is_premium, status, subscription_expires_at,
                        plan_name, billing_customer_id, billing_subscription_id,
                        note, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        normalized_username,
                        password_plain,
                        hash_password(password_plain),
                        max(1, int(bandwidth_mbps or 30)),
                        normalized_speed_mode or "limited",
                        1 if bool(is_premium) else 0,
                        normalized_status or "active",
                        to_iso(expires_at),
                        (plan_name or "custom").strip() or "custom",
                        (billing_customer_id or "").strip(),
                        (billing_subscription_id or "").strip(),
                        (note or "").strip(),
                        to_iso(now),
                        to_iso(now),
                    ),
                )
                self._upsert_access_key(
                    conn,
                    row_id,
                    normalized_username,
                    password_plain,
                    bool(is_premium),
                )

            row = self._resolve_user_row(conn, user_id=row_id)
            payload = self._serialize_api_user(conn, row)
            conn.commit()

        self.sync_fptn()
        return payload

    def extend_subscription_by_lookup(
        self,
        *,
        days: int,
        username: Optional[str] = None,
        billing_subscription_id: Optional[str] = None,
        billing_customer_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            row = self._resolve_user_row(
                conn,
                username=username,
                billing_subscription_id=billing_subscription_id,
                billing_customer_id=billing_customer_id,
            )
        self.extend_subscription(row["id"], days)
        return self.get_billing_user(
            username=row["username"],
            billing_subscription_id=billing_subscription_id,
            billing_customer_id=billing_customer_id,
        )

    def set_user_status_by_lookup(
        self,
        *,
        next_status: str,
        username: Optional[str] = None,
        billing_subscription_id: Optional[str] = None,
        billing_customer_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            row = self._resolve_user_row(
                conn,
                username=username,
                billing_subscription_id=billing_subscription_id,
                billing_customer_id=billing_customer_id,
            )
        self.set_user_status(row["id"], next_status)
        return self.get_billing_user(
            username=row["username"],
            billing_subscription_id=billing_subscription_id,
            billing_customer_id=billing_customer_id,
        )

    def update_user_speed_by_lookup(
        self,
        *,
        speed_mode: Optional[str] = None,
        bandwidth_mbps: Optional[int] = None,
        username: Optional[str] = None,
        billing_subscription_id: Optional[str] = None,
        billing_customer_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if speed_mode is None and bandwidth_mbps is None:
            raise ValueError("speed_mode or bandwidth_mbps is required.")

        with self.connect() as conn:
            row = self._resolve_user_row(
                conn,
                username=username,
                billing_subscription_id=billing_subscription_id,
                billing_customer_id=billing_customer_id,
            )
            next_speed_mode = (
                self._normalize_speed_mode(speed_mode)
                if speed_mode is not None
                else row["speed_mode"]
            )
            next_bandwidth = (
                max(1, int(bandwidth_mbps))
                if bandwidth_mbps is not None
                else int(row["bandwidth_mbps"])
            )
            conn.execute(
                """
                UPDATE users
                SET bandwidth_mbps = ?, speed_mode = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_bandwidth, next_speed_mode, to_iso(utcnow()), row["id"]),
            )
            conn.commit()

        self.sync_fptn()
        return self.get_billing_user(
            username=row["username"],
            billing_subscription_id=billing_subscription_id,
            billing_customer_id=billing_customer_id,
        )

    def rotate_access_key_by_lookup(
        self,
        *,
        username: Optional[str] = None,
        billing_subscription_id: Optional[str] = None,
        billing_customer_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            row = self._resolve_user_row(
                conn,
                username=username,
                billing_subscription_id=billing_subscription_id,
                billing_customer_id=billing_customer_id,
            )
        self.rotate_access_key(row["id"])
        return self.get_billing_user(
            username=row["username"],
            billing_subscription_id=billing_subscription_id,
            billing_customer_id=billing_customer_id,
        )

    def create_node(
        self,
        name: str,
        host: str,
        port: int,
        region: str,
        tier: str,
        md5_fingerprint: str,
    ) -> None:
        if tier not in {"public", "premium", "censored"}:
            raise ValueError("Unsupported node tier.")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO nodes (
                    id, name, host, port, region, tier, md5_fingerprint, status,
                    cpu_load, memory_load, network_rx_mbps, network_tx_mbps,
                    uptime_percent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'online', 10, 16, 0, 0, 100, ?)
                """,
                (
                    str(uuid.uuid4()),
                    name.strip(),
                    host.strip(),
                    int(port),
                    region.strip(),
                    tier,
                    md5_fingerprint.strip(),
                    to_iso(utcnow()),
                ),
            )
            self._refresh_all_access_keys(conn)
            conn.commit()
        self.sync_fptn()

    def delete_node(self, node_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            self._refresh_all_access_keys(conn)
            conn.commit()
        self.sync_fptn()

    def delete_node_by_agent_id(self, agent_id: str) -> bool:
        agent_id = (agent_id or "").strip()
        if not agent_id:
            raise ValueError("agent_id is required.")

        deleted = False
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM nodes WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if existing:
                conn.execute("DELETE FROM nodes WHERE agent_id = ?", (agent_id,))
                self._refresh_all_access_keys(conn)
                conn.commit()
                deleted = True

        if deleted:
            self.sync_fptn()
        return deleted

    def get_access_bundle(self, user_id: str) -> dict[str, str]:
        self.sync_fptn()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, username, password_plain, password_hash, is_premium
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
            if not row:
                raise ValueError("Access key not found.")

            if row["password_hash"] != hash_password(row["password_plain"]):
                conn.execute(
                    "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                    (hash_password(row["password_plain"]), to_iso(utcnow()), row["id"]),
                )

            self._upsert_access_key(
                conn,
                row["id"],
                row["username"],
                row["password_plain"],
                bool(row["is_premium"]),
            )
            conn.commit()

            row = conn.execute(
                """
                SELECT users.username, access_keys.token_payload
                FROM users
                JOIN access_keys ON access_keys.user_id = users.id
                WHERE users.id = ?
                """,
                (user_id,),
            ).fetchone()
            if not row:
                raise ValueError("Access key not found.")
            valid, reason = self._validate_token_payload(row["username"], row["token_payload"])
            if not valid:
                self.sync_fptn()
                with self.connect() as retry_conn:
                    retry_row = retry_conn.execute(
                        """
                        SELECT users.username, access_keys.token_payload
                        FROM users
                        JOIN access_keys ON access_keys.user_id = users.id
                        WHERE users.id = ?
                        """,
                        (user_id,),
                    ).fetchone()
                if not retry_row:
                    raise ValueError("Access key not found after re-sync.")
                valid, reason = self._validate_token_payload(
                    retry_row["username"],
                    retry_row["token_payload"],
                )
                if not valid:
                    raise ValueError(f"Access bundle is inconsistent: {reason}")
                return {
                    "username": retry_row["username"],
                    "token_payload": retry_row["token_payload"],
                }
            return {"username": row["username"], "token_payload": row["token_payload"]}

    def sync_fptn(self) -> None:
        with self.connect() as conn:
            self._enforce_subscription_state(conn)
            self._repair_user_hashes(conn)
            speed_policy = self._speed_policy(self._get_meta(conn))
            active_users = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT username, password_hash, bandwidth_mbps, speed_mode, is_premium
                    FROM users
                    WHERE status = 'active'
                    ORDER BY username ASC
                    """
                )
            ]
            serialized_users = [
                {
                    **user,
                    "bandwidth_mbps": self._effective_bandwidth_mbps(user, speed_policy),
                }
                for user in active_users
            ]
            public_nodes, premium_nodes, censored_nodes = self._current_nodes(conn)
            nodes = sorted(
                public_nodes + premium_nodes + censored_nodes,
                key=lambda node: (node["tier"], node["name"]),
            )
            self._refresh_all_access_keys(conn)
            write_fptn_config(
                self.settings.fptn_config_dir,
                self.settings.service_name,
                serialized_users,
                nodes,
            )
            self._set_meta(conn, "last_sync_at", to_iso(utcnow()))
            conn.commit()
