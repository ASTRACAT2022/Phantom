#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit
from urllib.error import URLError
from urllib.request import Request, urlopen

try:
    import grpc
except ImportError:  # pragma: no cover - depends on deployment env
    grpc = None


NODE_AGENT_GRPC_SERVICE = "phantom.nodeagent.NodeAgentService"
NODE_AGENT_GRPC_GET_CONFIG = f"/{NODE_AGENT_GRPC_SERVICE}/GetConfig"
NODE_AGENT_GRPC_HEARTBEAT = f"/{NODE_AGENT_GRPC_SERVICE}/Heartbeat"
NODE_AGENT_GRPC_DEREGISTER = f"/{NODE_AGENT_GRPC_SERVICE}/Deregister"


DEFAULT_ENV_PATH = "/etc/phantom-node-controller.env"


def load_env_file(path: str) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass
class AgentConfig:
    panel_url: str
    shared_token: str
    transport: str
    grpc_target: str
    agent_id: str
    node_name: str
    node_host: str
    node_port: int
    region: str
    tier: str
    cert_path: str
    fptn_config_dir: str
    metrics_url: str
    interface: str
    interval_seconds: int
    request_timeout: int


class LinuxCollector:
    def __init__(self, interface: str) -> None:
        self.interface = interface or self.detect_default_interface() or "eth0"
        self._previous_network = None

    def detect_default_interface(self) -> Optional[str]:
        route_path = Path("/proc/net/route")
        if not route_path.exists():
            return None
        for line in route_path.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split()
            if len(parts) > 2 and parts[1] == "00000000":
                return parts[0]
        return None

    def read_uptime_seconds(self) -> int:
        return int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))

    def read_loadavg(self) -> tuple[float, float, float]:
        parts = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        return float(parts[0]), float(parts[1]), float(parts[2])

    def read_cpu_percent(self) -> float:
        def snapshot() -> tuple[int, int]:
            fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
            values = [int(value) for value in fields]
            idle = values[3] + values[4]
            total = sum(values)
            return idle, total

        idle_1, total_1 = snapshot()
        time.sleep(0.2)
        idle_2, total_2 = snapshot()
        delta_total = total_2 - total_1
        delta_idle = idle_2 - idle_1
        if delta_total <= 0:
            return 0.0
        return round((1 - (delta_idle / delta_total)) * 100, 1)

    def read_memory(self) -> tuple[int, int, float]:
        values = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw_value = line.split(":", 1)
            values[key] = int(raw_value.strip().split()[0])
        total_mb = values.get("MemTotal", 0) // 1024
        available_mb = values.get("MemAvailable", 0) // 1024
        used_mb = max(total_mb - available_mb, 0)
        used_percent = round((used_mb / total_mb) * 100, 1) if total_mb else 0.0
        return total_mb, used_mb, used_percent

    def read_disk_used_percent(self) -> float:
        usage = shutil.disk_usage("/")
        if not usage.total:
            return 0.0
        return round((usage.used / usage.total) * 100, 1)

    def read_network_counters(self) -> tuple[int, int]:
        for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
            if ":" not in line:
                continue
            name, raw_data = line.split(":", 1)
            if name.strip() != self.interface:
                continue
            fields = raw_data.split()
            return int(fields[0]), int(fields[8])
        return 0, 0

    def read_network_speed(self) -> tuple[float, float, int, int]:
        rx_total, tx_total = self.read_network_counters()
        now = time.time()
        if self._previous_network is None:
            self._previous_network = (now, rx_total, tx_total)
            return 0.0, 0.0, rx_total, tx_total
        previous_time, previous_rx, previous_tx = self._previous_network
        elapsed = max(now - previous_time, 1.0)
        rx_mbps = max(rx_total - previous_rx, 0) * 8 / elapsed / 1_000_000
        tx_mbps = max(tx_total - previous_tx, 0) * 8 / elapsed / 1_000_000
        self._previous_network = (now, rx_total, tx_total)
        return round(rx_mbps, 2), round(tx_mbps, 2), rx_total, tx_total

    def count_connections(self, port: int) -> int:
        try:
            result = subprocess.run(
                ["ss", "-Htan"],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return 0
            target = f":{port}"
            count = 0
            for line in result.stdout.splitlines():
                if "ESTAB" not in line:
                    continue
                if target in line:
                    count += 1
            return count
        except FileNotFoundError:
            return 0


def fingerprint_from_cert(cert_path: str) -> str:
    if not cert_path or not Path(cert_path).exists():
        return ""
    try:
        result = subprocess.run(
            [
                "openssl",
                "x509",
                "-noout",
                "-fingerprint",
                "-md5",
                "-in",
                cert_path,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or "=" not in result.stdout:
            return ""
        return result.stdout.split("=", 1)[1].strip().replace(":", "").lower()
    except FileNotFoundError:
        return ""


def fetch_metrics_payload(metrics_url: str, timeout: int) -> str:
    if not metrics_url:
        return ""
    request_kwargs = {"timeout": timeout}
    parsed_url = urlsplit(metrics_url)
    if parsed_url.scheme == "https" and (parsed_url.hostname or "").strip().lower() in {"127.0.0.1", "localhost", "::1"}:
        request_kwargs["context"] = ssl._create_unverified_context()
    with urlopen(metrics_url, **request_kwargs) as response:
        return response.read().decode("utf-8")


def metrics_look_like_fptn(payload: str) -> bool:
    if not payload:
        return False
    return "fptn_active_sessions" in payload or "fptn_user_" in payload


def read_fptn_active_sessions(metrics_url: str, timeout: int) -> int:
    if not metrics_url:
        return 0
    try:
        payload = fetch_metrics_payload(metrics_url, timeout)
    except Exception:
        return 0
    for line in payload.splitlines():
        line = line.strip()
        if line.startswith("fptn_active_sessions "):
            try:
                return int(float(line.split()[-1]))
            except ValueError:
                return 0
    return 0


def _grpc_request(target: str, method: str, payload: dict, shared_token: str, timeout: int) -> dict:
    if grpc is None:
        raise RuntimeError("grpcio is not installed. Install grpcio to use gRPC transport.")

    with grpc.insecure_channel(target) as channel:
        rpc = channel.unary_unary(
            method,
            request_serializer=lambda value: json.dumps(value).encode("utf-8"),
            response_deserializer=lambda raw: json.loads(raw.decode("utf-8")) if raw else {},
        )
        return rpc(
            payload,
            timeout=timeout,
            metadata=(("authorization", f"Bearer {shared_token}"),),
        )


def _derive_grpc_target(panel_url: str, grpc_target: str, grpc_port: int) -> str:
    if grpc_target:
        return grpc_target
    parsed = urlsplit(panel_url)
    host = parsed.hostname or panel_url
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{grpc_port}"


def fetch_panel_defaults_http(panel_url: str, shared_token: str, timeout: int) -> dict:
    request = Request(
        f"{panel_url}/api/node-agent/config",
        headers={"Authorization": f"Bearer {shared_token}"},
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_panel_defaults_grpc(grpc_target: str, shared_token: str, timeout: int) -> dict:
    return _grpc_request(grpc_target, NODE_AGENT_GRPC_GET_CONFIG, {}, shared_token, timeout)


def build_config() -> AgentConfig:
    load_env_file(os.getenv("PHANTOM_NODE_ENV_FILE", DEFAULT_ENV_PATH))
    hostname = socket.gethostname()
    panel_url = os.getenv("PHANTOM_PANEL_URL", "http://127.0.0.1:8000").rstrip("/")
    shared_token = os.getenv("PHANTOM_SHARED_TOKEN", "phantom-node-shared-token")
    transport = os.getenv("PHANTOM_NODE_TRANSPORT", "http").strip().lower() or "http"
    request_timeout = int(os.getenv("PHANTOM_REQUEST_TIMEOUT", "5"))
    grpc_port = int(os.getenv("PHANTOM_PANEL_GRPC_PORT", "50061"))
    grpc_target = _derive_grpc_target(
        panel_url,
        os.getenv("PHANTOM_PANEL_GRPC_TARGET", "").strip(),
        grpc_port,
    )

    if transport not in {"http", "grpc"}:
        raise RuntimeError("PHANTOM_NODE_TRANSPORT must be 'http' or 'grpc'.")

    try:
        if transport == "grpc":
            panel_defaults = fetch_panel_defaults_grpc(grpc_target, shared_token, request_timeout)
        else:
            panel_defaults = fetch_panel_defaults_http(panel_url, shared_token, request_timeout)
    except Exception:
        panel_defaults = {}

    raw_port = os.getenv("FPTN_NODE_PORT", "").strip()
    raw_region = os.getenv("FPTN_NODE_REGION", "").strip()
    raw_tier = os.getenv("FPTN_NODE_TIER", "").strip()
    raw_host = os.getenv("FPTN_NODE_HOST", "").strip()

    return AgentConfig(
        panel_url=panel_url,
        shared_token=shared_token,
        transport=transport,
        grpc_target=grpc_target,
        agent_id=os.getenv("PHANTOM_AGENT_ID", hostname),
        node_name=os.getenv("FPTN_NODE_NAME", hostname),
        node_host=raw_host or panel_defaults.get("host", "") or hostname,
        node_port=int(raw_port or panel_defaults.get("port", 8443)),
        region=raw_region or panel_defaults.get("region", "Unknown"),
        tier=raw_tier or panel_defaults.get("tier", "public"),
        cert_path=os.getenv("FPTN_CERT_PATH", "/etc/fptn/server.crt"),
        fptn_config_dir=os.getenv("FPTN_CONFIG_DIR", "/etc/fptn"),
        metrics_url=os.getenv("LOCAL_FPTN_METRICS_URL", ""),
        interface=os.getenv("PHANTOM_NET_INTERFACE", ""),
        interval_seconds=int(os.getenv("PHANTOM_HEARTBEAT_INTERVAL", "30")),
        request_timeout=request_timeout,
    )


def post_heartbeat_http(config: AgentConfig, payload: dict) -> None:
    request = Request(
        f"{config.panel_url}/api/node-agent/heartbeat",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.shared_token}",
        },
        method="POST",
    )
    with urlopen(request, timeout=config.request_timeout) as response:
        response.read()


def post_heartbeat_grpc(config: AgentConfig, payload: dict) -> None:
    _grpc_request(
        config.grpc_target,
        NODE_AGENT_GRPC_HEARTBEAT,
        payload,
        config.shared_token,
        config.request_timeout,
    )


def post_heartbeat(config: AgentConfig, payload: dict) -> None:
    if config.transport == "grpc":
        post_heartbeat_grpc(config, payload)
        return
    post_heartbeat_http(config, payload)


def deregister_node(config: AgentConfig, agent_id: str) -> dict:
    payload = {"agent_id": agent_id}
    if config.transport == "grpc":
        return _grpc_request(
            config.grpc_target,
            NODE_AGENT_GRPC_DEREGISTER,
            payload,
            config.shared_token,
            config.request_timeout,
        )

    request = Request(
        f"{config.panel_url}/api/node-agent/deregister",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.shared_token}",
        },
        method="POST",
    )
    with urlopen(request, timeout=config.request_timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def build_payload(config: AgentConfig, collector: LinuxCollector) -> dict:
    if not Path("/proc").exists():
        raise RuntimeError("This node-controller currently targets Linux hosts with /proc.")

    cpu_percent = collector.read_cpu_percent()
    load_1, load_5, load_15 = collector.read_loadavg()
    memory_total_mb, memory_used_mb, memory_used_percent = collector.read_memory()
    disk_used_percent = collector.read_disk_used_percent()
    network_rx_mbps, network_tx_mbps, rx_bytes_total, tx_bytes_total = collector.read_network_speed()
    active_sessions = read_fptn_active_sessions(config.metrics_url, config.request_timeout)
    connections_current = collector.count_connections(config.node_port)
    hostname = socket.gethostname()

    return {
        "agent_id": config.agent_id,
        "node": {
            "name": config.node_name,
            "host": config.node_host,
            "port": config.node_port,
            "region": config.region,
            "tier": config.tier,
            "md5_fingerprint": fingerprint_from_cert(config.cert_path),
            "hostname": hostname,
        },
        "system": {
            "hostname": hostname,
            "uptime_seconds": collector.read_uptime_seconds(),
            "load1": load_1,
            "load5": load_5,
            "load15": load_15,
            "cpu_percent": cpu_percent,
            "memory_total_mb": memory_total_mb,
            "memory_used_mb": memory_used_mb,
            "memory_used_percent": memory_used_percent,
            "disk_used_percent": disk_used_percent,
            "network_rx_mbps": network_rx_mbps,
            "network_tx_mbps": network_tx_mbps,
            "rx_bytes_total": rx_bytes_total,
            "tx_bytes_total": tx_bytes_total,
            "connections_current": max(connections_current, active_sessions),
            "fptn_active_sessions": active_sessions,
        },
    }


def _panel_config_check(config: AgentConfig) -> tuple[bool, str]:
    try:
        if config.transport == "grpc":
            payload = fetch_panel_defaults_grpc(config.grpc_target, config.shared_token, config.request_timeout)
            return True, f"gRPC defaults loaded from {config.grpc_target} ({payload.get('host', 'n/a')}:{payload.get('port', 'n/a')})"
        payload = fetch_panel_defaults_http(config.panel_url, config.shared_token, config.request_timeout)
        return True, f"HTTP defaults loaded from {config.panel_url} ({payload.get('host', 'n/a')}:{payload.get('port', 'n/a')})"
    except Exception as exc:
        return False, str(exc)


def _tcp_check(host: str, port: int, timeout: int) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=max(timeout, 1)):
            return True, f"TCP connect ok: {host}:{port}"
    except Exception as exc:
        return False, str(exc)


def _metrics_check(config: AgentConfig) -> tuple[bool, str]:
    if not config.metrics_url:
        return False, "LOCAL_FPTN_METRICS_URL is empty"
    try:
        payload = fetch_metrics_payload(config.metrics_url, config.request_timeout)
    except Exception as exc:
        return False, str(exc)
    if not metrics_look_like_fptn(payload):
        return False, "metrics endpoint returned non-FPTN payload"
    return True, f"metrics ok: {config.metrics_url}"


def _users_list_check(config: AgentConfig) -> tuple[bool, str]:
    users_path = Path(config.fptn_config_dir) / "users.list"
    if not users_path.exists():
        return False, f"missing {users_path}"
    try:
        lines = [
            line.strip()
            for line in users_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception as exc:
        return False, str(exc)
    if not lines:
        return False, f"{users_path} is empty"
    return True, f"{len(lines)} active user(s) in {users_path}"


def build_self_check(config: AgentConfig, collector: LinuxCollector) -> dict:
    fingerprint = fingerprint_from_cert(config.cert_path)
    panel_ok, panel_message = _panel_config_check(config)
    port_ok, port_message = _tcp_check("127.0.0.1", config.node_port, config.request_timeout)
    metrics_ok, metrics_message = _metrics_check(config)
    users_ok, users_message = _users_list_check(config)
    checks = {
        "panel": {"ok": panel_ok, "message": panel_message},
        "certificate": {
            "ok": bool(fingerprint),
            "message": f"fingerprint={fingerprint}" if fingerprint else f"certificate unreadable: {config.cert_path}",
        },
        "public_port": {"ok": port_ok, "message": port_message},
        "metrics": {"ok": metrics_ok, "message": metrics_message},
        "users_list": {"ok": users_ok, "message": users_message},
    }
    payload = build_payload(config, collector)
    ok = all(item["ok"] for item in checks.values())
    return {
        "ok": ok,
        "checks": checks,
        "node": payload["node"],
        "system": payload["system"],
    }


def run_once(config: AgentConfig, collector: LinuxCollector) -> int:
    payload = build_payload(config, collector)
    post_heartbeat(config, payload)
    print(json.dumps(payload, indent=2))
    return 0


def run_deregister(config: AgentConfig, agent_id: str) -> int:
    payload = deregister_node(config, agent_id)
    print(json.dumps(payload, indent=2))
    return 0


def run_self_check(config: AgentConfig, collector: LinuxCollector) -> int:
    payload = build_self_check(config, collector)
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def run_forever(config: AgentConfig, collector: LinuxCollector) -> int:
    while True:
        try:
            payload = build_payload(config, collector)
            post_heartbeat(config, payload)
            print(
                f"[heartbeat] sent node={config.node_name} "
                f"rx={payload['system']['network_rx_mbps']}Mbps "
                f"tx={payload['system']['network_tx_mbps']}Mbps "
                f"sessions={payload['system']['fptn_active_sessions']}"
            )
        except URLError as exc:
            print(f"[heartbeat] panel unreachable: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"[heartbeat] failed: {exc}", file=sys.stderr)
        time.sleep(max(config.interval_seconds, 5))


def main() -> int:
    parser = argparse.ArgumentParser(description="Phantom lightweight node-controller for FPTN.")
    parser.add_argument("--once", action="store_true", help="Send one heartbeat and exit.")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run local health checks for panel, cert, metrics, users.list and TCP port.",
    )
    parser.add_argument(
        "--deregister-agent-id",
        default="",
        help="Remove an existing node registration by agent_id and exit.",
    )
    args = parser.parse_args()

    config = build_config()
    if args.deregister_agent_id:
        return run_deregister(config, args.deregister_agent_id)

    collector = LinuxCollector(config.interface)
    if args.once:
        return run_once(config, collector)
    if args.self_check:
        return run_self_check(config, collector)
    return run_forever(config, collector)


if __name__ == "__main__":
    raise SystemExit(main())
