"""无第三方依赖的责任认定 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import LiabilityError, ValidationFailed
from .service import LiabilityDeterminationService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class ThreadLocalServiceProvider:
    """为每个工作线程提供独立的 SQLite 连接与服务实例。

    SQLite 连接不能跨线程共享事务；按线程隔离后，多请求并发写由
    BEGIN IMMEDIATE + busy_timeout 串行化，既有线程安全又不会交错事务。
    """

    def __init__(self, database: Path | str) -> None:
        self.database = str(database)
        self._local = threading.local()
        self._connections: list = []
        self._lock = threading.Lock()

    def get(self) -> LiabilityDeterminationService:
        service = getattr(self._local, "service", None)
        if service is None:
            connection = connect(self.database, check_same_thread=False)
            with self._lock:
                self._connections.append(connection)
            service = LiabilityDeterminationService(connection)
            self._local.service = service
        return service

    def close(self) -> None:
        with self._lock:
            for connection in self._connections:
                connection.close()
            self._connections.clear()


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(
        self,
        service: LiabilityDeterminationService | None = None,
        *,
        service_provider: ThreadLocalServiceProvider | None = None,
    ) -> None:
        self._static_service = service
        self._service_provider = service_provider

    @property
    def service(self) -> LiabilityDeterminationService:
        if self._service_provider is not None:
            return self._service_provider.get()
        assert self._static_service is not None
        return self._static_service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            # 用户引导发生在任何登录身份之前，不要求 X-Actor-Id
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                ))
            actor = self._actor(normalized_headers)
            if method == "POST" and path == "/cases":
                return Response(201, self.service.create_case(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "cases":
                return Response(200, self.service.get_case(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "cases" and parts[2] == "parties":
                return Response(201, self.service.add_party(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "cases" and parts[2] == "materials":
                return Response(201, self.service.add_material(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "cases" and parts[2] == "draft":
                return Response(201, self.service.create_draft(actor, parts[1], payload))
            if method == "PUT" and len(parts) == 3 and parts[0] == "cases" and parts[2] == "draft":
                return Response(200, self.service.save_draft(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "cases" and parts[2] == "submit":
                content = payload.get("content")
                return Response(200, self.service.submit_draft(
                    actor, parts[1], None if content is None else content
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "cases" and parts[2] == "revise":
                return Response(201, self.service.revise_draft(
                    actor, parts[1], payload["content"], payload["change_summary"]
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "cases" and parts[2] == "sign":
                return Response(200, self.service.sign(
                    actor, parts[1], payload["level"], payload["opinion"], payload.get("comment", "")
                ))
            if method == "GET" and len(parts) == 2 and parts[0] == "determinations":
                return Response(200, self.service.get_determination(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "determinations":
                return Response(200, self.service.get_version(actor, parts[1], int(parts[2])))
            if method == "GET" and len(parts) == 3 and parts[0] == "cases" and parts[2] == "audit":
                return Response(200, {"events": self.service.audit_events(actor, parts[1])})
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except LiabilityError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LiabilityDetermination/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动交通事故责任认定 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("liability_determination.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    provider = ThreadLocalServiceProvider(args.database)
    application = JsonApplication(service_provider=provider)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        provider.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
