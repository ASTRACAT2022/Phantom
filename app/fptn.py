import base64
import hashlib
import json
import random
import string
from pathlib import Path
from typing import Iterable


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def generate_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def normalize_username(username: str) -> str:
    normalized = "".join(ch for ch in username if ch.isalnum()).lower()
    return normalized


def build_server_entry(node: dict) -> dict:
    return {
        "name": node["name"],
        "host": node["host"],
        "md5_fingerprint": node["md5_fingerprint"] or "",
        "port": int(node["port"]),
    }


def build_access_token(
    service_name: str,
    username: str,
    password: str,
    public_nodes: Iterable[dict],
    premium_nodes: Iterable[dict],
    censored_nodes: Iterable[dict],
    is_premium: bool,
) -> str:
    public_servers = [build_server_entry(node) for node in public_nodes]
    premium_servers = [build_server_entry(node) for node in premium_nodes]
    censored_servers = [build_server_entry(node) for node in censored_nodes]
    servers = premium_servers + public_servers if is_premium else public_servers
    payload = {
        "version": 1,
        "service_name": service_name,
        "username": username,
        "password": password,
        "servers": servers,
        "censored_zone_servers": censored_servers,
    }
    return json.dumps(payload, separators=(",", ":"))


def build_access_link(token_payload: str) -> str:
    encoded = base64.b64encode(token_payload.encode("utf-8")).decode("utf-8")
    return "fptn:" + encoded.replace("=", "")


def write_fptn_config(
    config_dir: Path,
    service_name: str,
    users: list[dict],
    nodes: list[dict],
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)

    public_nodes = [
        node for node in nodes if node["tier"] == "public" and node["status"] != "offline"
    ]
    premium_nodes = [
        node
        for node in nodes
        if node["tier"] == "premium" and node["status"] != "offline"
    ]
    censored_nodes = [
        node
        for node in nodes
        if node["tier"] == "censored" and node["status"] != "offline"
    ]

    users_lines = []
    for user in users:
        premium_flag = "1" if user["is_premium"] else "0"
        users_lines.append(
            f'{user["username"]} {user["password_hash"]} {int(user["bandwidth_mbps"])} {premium_flag}'
        )

    (config_dir / "users.list").write_text(
        "\n".join(users_lines) + ("\n" if users_lines else ""),
        encoding="utf-8",
    )
    (config_dir / "service_name.txt").write_text(service_name, encoding="utf-8")
    (config_dir / "servers.json").write_text(
        json.dumps([build_server_entry(node) for node in public_nodes], indent=2),
        encoding="utf-8",
    )
    (config_dir / "premium_servers.json").write_text(
        json.dumps([build_server_entry(node) for node in premium_nodes], indent=2),
        encoding="utf-8",
    )
    (config_dir / "servers_censored_zone.json").write_text(
        json.dumps([build_server_entry(node) for node in censored_nodes], indent=2),
        encoding="utf-8",
    )
