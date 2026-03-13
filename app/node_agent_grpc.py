import json
from typing import Any, Optional

try:
    import grpc
except ImportError:  # pragma: no cover - exercised in environments without grpcio
    grpc = None


NODE_AGENT_GRPC_SERVICE = "phantom.nodeagent.NodeAgentService"
NODE_AGENT_GRPC_GET_CONFIG = f"/{NODE_AGENT_GRPC_SERVICE}/GetConfig"
NODE_AGENT_GRPC_HEARTBEAT = f"/{NODE_AGENT_GRPC_SERVICE}/Heartbeat"
NODE_AGENT_GRPC_DEREGISTER = f"/{NODE_AGENT_GRPC_SERVICE}/Deregister"


def _deserialize_payload(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _serialize_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _metadata_value(context: Any, key: str) -> Optional[str]:
    target = key.lower()
    for item in context.invocation_metadata():
        if item.key.lower() == target:
            return item.value
    return None


async def _abort(context: Any, code: Any, detail: str) -> None:
    await context.abort(code, detail)


class NodeAgentGrpcService:
    def __init__(self, settings: Any, service: Any) -> None:
        self.settings = settings
        self.service = service

    async def _verify(self, context: Any) -> None:
        authorization = _metadata_value(context, "authorization")
        if authorization != f"Bearer {self.settings.node_agent_token}":
            await _abort(context, grpc.StatusCode.UNAUTHENTICATED, "Unauthorized node agent.")

    async def get_config(self, request: dict[str, Any], context: Any) -> dict[str, Any]:
        await self._verify(context)
        return self.service.get_node_agent_config()

    async def heartbeat(self, request: dict[str, Any], context: Any) -> dict[str, Any]:
        await self._verify(context)
        try:
            return self.service.ingest_node_heartbeat(request)
        except ValueError as exc:
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise

    async def deregister(self, request: dict[str, Any], context: Any) -> dict[str, Any]:
        await self._verify(context)
        try:
            deleted = self.service.delete_node_by_agent_id(request.get("agent_id", ""))
        except ValueError as exc:
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise
        return {"ok": True, "deleted": deleted, "agent_id": request.get("agent_id", "")}


def build_node_agent_grpc_handler(settings: Any, service: Any) -> Any:
    if grpc is None:
        raise RuntimeError("grpcio is not installed. Install dependencies and redeploy.")

    servicer = NodeAgentGrpcService(settings, service)
    return grpc.method_handlers_generic_handler(
        NODE_AGENT_GRPC_SERVICE,
        {
            "GetConfig": grpc.unary_unary_rpc_method_handler(
                servicer.get_config,
                request_deserializer=_deserialize_payload,
                response_serializer=_serialize_payload,
            ),
            "Heartbeat": grpc.unary_unary_rpc_method_handler(
                servicer.heartbeat,
                request_deserializer=_deserialize_payload,
                response_serializer=_serialize_payload,
            ),
            "Deregister": grpc.unary_unary_rpc_method_handler(
                servicer.deregister,
                request_deserializer=_deserialize_payload,
                response_serializer=_serialize_payload,
            ),
        },
    )


async def start_node_agent_grpc_server(settings: Any, service: Any) -> Any:
    if grpc is None:
        raise RuntimeError("grpcio is not installed. Install dependencies and redeploy.")

    server = grpc.aio.server()
    server.add_generic_rpc_handlers((build_node_agent_grpc_handler(settings, service),))
    listen_addr = f"{settings.node_agent_grpc_host}:{settings.node_agent_grpc_port}"
    server.add_insecure_port(listen_addr)
    await server.start()
    return server
