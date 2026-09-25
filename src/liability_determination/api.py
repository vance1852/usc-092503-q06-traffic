"""责任认定服务的无第三方依赖 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ServiceError, ValidationFailed
from .service import LiabilityDeterminationService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到责任认定领域服务，便于无网络单元测试。"""

    def __init__(self, service: LiabilityDeterminationService) -> None:
        self.service = service
        # SQLite 连接在多线程 HTTP 服务中共享，用锁串行化每个请求。
        self._lock = threading.RLock()

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

    @staticmethod
    def _opinion(payload: dict[str, Any]) -> str:
        opinion = payload.get("opinion", "")
        if not isinstance(opinion, str) or not opinion.strip():
            raise ValidationFailed("opinion 不能为空")
        return opinion

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            with self._lock:
                return self._handle(method, path, parts, normalized_headers, body)
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})

    def _handle(
        self, method: str, path: str, parts: list[str],
        normalized_headers: Mapping[str, str], body: bytes,
    ) -> Response:
        actor = lambda: self._actor(normalized_headers)
        if method == "GET" and path == "/health":
            return Response(200, {"status": "ok"})
        payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
        if method == "POST" and path == "/users":
            result = self.service.create_user(
                payload["user_id"], payload["display_name"], payload["role"]
            )
            return Response(201, result)
        if method == "POST" and path == "/accidents":
            return Response(201, self.service.register_accident(actor(), payload))
        if method == "GET" and len(parts) == 2 and parts[0] == "accidents":
            return Response(200, self.service.accident(actor(), parts[1]))
        if method == "POST" and len(parts) == 3 and parts[0] == "accidents" and parts[2] == "parties":
            result = self.service.add_party(
                actor(), parts[1], payload["party_id"], payload["name"],
                payload["party_kind"], payload.get("contact"),
            )
            return Response(201, result)
        if method == "POST" and len(parts) == 3 and parts[0] == "accidents" and parts[2] == "drafts":
            return Response(201, self.service.create_draft(actor(), parts[1], payload))
        if method == "POST" and len(parts) == 3 and parts[0] == "drafts" and parts[2] == "edit":
            return Response(200, self.service.edit_draft(actor(), parts[1], payload))
        if method == "POST" and len(parts) == 3 and parts[0] == "drafts" and parts[2] == "revise":
            result = self.service.revise_draft(
                actor(), parts[1], payload, payload["trigger_reason"], payload.get("change_note")
            )
            return Response(201, result)
        if method == "POST" and len(parts) == 3 and parts[0] == "drafts" and parts[2] == "submit":
            return Response(200, self.service.submit_draft(actor(), parts[1], self._opinion(payload)))
        if method == "POST" and len(parts) == 3 and parts[0] == "drafts" and parts[2] == "review":
            result = self.service.review_draft(
                actor(), parts[1], bool(payload.get("approve", True)), self._opinion(payload)
            )
            return Response(200, result)
        if method == "POST" and len(parts) == 3 and parts[0] == "drafts" and parts[2] == "final":
            return Response(200, self.service.final_sign(actor(), parts[1], self._opinion(payload)))
        if method == "GET" and len(parts) == 2 and parts[0] == "drafts":
            return Response(200, self.service.version_detail(actor(), parts[1], self._current_no(parts[1])))
        if method == "GET" and len(parts) == 3 and parts[0] == "drafts" and parts[2] == "history":
            return Response(200, self.service.history(actor(), parts[1]))
        if method == "GET" and len(parts) == 3 and parts[0] == "drafts" and parts[2] == "explanation":
            return Response(200, self.service.explanation(actor(), parts[1]))
        if method == "POST" and len(parts) == 3 and parts[0] == "drafts" and parts[2] == "document":
            return Response(200, self.service.service_document(actor(), parts[1]))
        return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})

    def _current_no(self, draft_id: str) -> int:
        row = self.service.connection.execute(
            "SELECT current_version_no FROM liability_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if row is None:
            raise ValidationFailed(f"责任认定草案不存在: {draft_id}")
        return int(row["current_version_no"])


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LiabilityDetermination/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
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
    parser.add_argument("--database", type=Path, default=Path("liability.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(LiabilityDeterminationService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
