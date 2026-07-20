from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from dashboard_backend.database import DashboardStore
from dashboard_backend.proxmox_gateway import create_gateway
from dashboard_backend.service import ConflictError, DashboardService, NotFoundError, ValidationError


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DATA_ROOT = ROOT / "data"
MAX_BODY = 2 * 1024 * 1024


class DashboardHandler(SimpleHTTPRequestHandler):
    server_version = "DemoExamDashboard/1.0"
    service: DashboardService
    admin_token: str

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, message: str, *args: Any) -> None:
        sys.stdout.write(f"[dashboard] {self.address_string()} - {message % args}\n")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        super().end_headers()

    def do_GET(self) -> None:
        if self.path.startswith("/api/"):
            self._handle_api("GET")
            return
        parsed = urlparse(self.path)
        if parsed.path != "/" and not (WEB_ROOT / parsed.path.lstrip("/")).is_file():
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        self._handle_api("POST")

    def do_PATCH(self) -> None:
        self._handle_api("PATCH")

    def do_DELETE(self) -> None:
        self._handle_api("DELETE")

    def _authorized(self, method: str, *, sensitive: bool = False) -> bool:
        if (method == "GET" and not sensitive) or not self.admin_token:
            return True
        return secrets_compare(self.headers.get("X-Admin-Token", ""), self.admin_token)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValidationError("Некорректный Content-Length") from exc
        if length < 0:
            raise ValidationError("Content-Length не может быть отрицательным")
        if length > MAX_BODY:
            raise ValidationError("Тело запроса слишком большое")
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Ожидался корректный JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("JSON должен быть объектом")
        return value

    def _json(self, payload: Any, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _handle_api(self, method: str) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        query = parse_qs(parsed.query)
        sensitive = method == "GET" and bool(parts) and parts[-1] == "credentials"
        if not self._authorized(method, sensitive=sensitive):
            self._json({"error": "Неверный административный токен", "code": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            if parts == ["api", "health"] and method == "GET":
                self._json({"status": "ok", "integration": self.service.integration()})
            elif parts == ["api", "bootstrap"] and method == "GET":
                self._json(self.service.bootstrap())
            elif parts == ["api", "overview"] and method == "GET":
                self._json(self.service.overview())
            elif parts == ["api", "metrics"] and method == "GET":
                self._json(self.service.metrics())
            elif parts == ["api", "activity"] and method == "GET":
                self._json(self.service.activity(int(query.get("limit", [30])[0])))
            elif parts == ["api", "blueprints"] and method == "GET":
                self._json(self.service.list_blueprints())
            elif parts == ["api", "blueprints"] and method == "POST":
                self._json(self.service.save_blueprint(self._body()), HTTPStatus.CREATED)
            elif len(parts) == 3 and parts[:2] == ["api", "blueprints"] and method == "GET":
                self._json(self.service.get_blueprint(int(parts[2])))
            elif len(parts) == 3 and parts[:2] == ["api", "blueprints"] and method == "PATCH":
                self._json(self.service.save_blueprint(self._body(), int(parts[2])))
            elif len(parts) == 3 and parts[:2] == ["api", "blueprints"] and method == "DELETE":
                self.service.delete_blueprint(int(parts[2])); self._json({"ok": True})
            elif len(parts) == 4 and parts[:2] == ["api", "blueprints"] and parts[3] == "duplicate" and method == "POST":
                self._json(self.service.duplicate_blueprint(int(parts[2])), HTTPStatus.CREATED)
            elif parts == ["api", "stands"] and method == "GET":
                self._json(self.service.list_stands())
            elif parts == ["api", "stands"] and method == "POST":
                self._json(self.service.create_stand(self._body()), HTTPStatus.ACCEPTED)
            elif parts == ["api", "pools"] and method == "GET":
                self._json(self.service.list_pools())
            elif parts == ["api", "pools", "import"] and method == "POST":
                self._json(self.service.import_pool(self._body()), HTTPStatus.CREATED)
            elif len(parts) == 3 and parts[:2] == ["api", "stands"] and method == "GET":
                self._json(self.service.get_stand(int(parts[2])))
            elif len(parts) == 3 and parts[:2] == ["api", "stands"] and method == "PATCH":
                self._json(self.service.update_stand(int(parts[2]), self._body()))
            elif len(parts) == 3 and parts[:2] == ["api", "stands"] and method == "DELETE":
                self.service.delete_stand(int(parts[2])); self._json({"ok": True})
            elif len(parts) == 4 and parts[:2] == ["api", "stands"] and parts[3] == "actions" and method == "POST":
                body = self._body(); self._json(self.service.stand_action(int(parts[2]), str(body.get("action", "")), body))
            elif len(parts) == 4 and parts[:2] == ["api", "stands"] and parts[3] == "credentials" and method == "GET":
                self._json(self.service.stand_credentials(int(parts[2])))
            elif len(parts) == 6 and parts[:2] == ["api", "stands"] and parts[3] == "vms" and parts[5] == "actions" and method == "POST":
                body = self._body()
                self._json(self.service.vm_action(int(parts[2]), int(parts[4]), str(body.get("action", "")), body))
            elif len(parts) == 6 and parts[:2] == ["api", "stands"] and parts[3] == "vms" and parts[5] == "credentials" and method == "GET":
                self._json(self.service.vm_credentials(int(parts[2]), int(parts[4])))
            elif parts == ["api", "checks"] and method == "GET":
                self._json(self.service.list_checks())
            elif parts == ["api", "checks", "run"] and method == "POST":
                body = self._body(); self._json(self.service.start_check(int(body.get("stand_id"))), HTTPStatus.ACCEPTED)
            else:
                self._json({"error": "Маршрут API не найден", "code": "not_found"}, HTTPStatus.NOT_FOUND)
        except NotFoundError as exc:
            self._json({"error": str(exc), "code": "not_found"}, HTTPStatus.NOT_FOUND)
        except ConflictError as exc:
            self._json({"error": str(exc), "code": "conflict"}, HTTPStatus.CONFLICT)
        except (ValidationError, ValueError, TypeError) as exc:
            self._json({"error": str(exc), "code": "validation_error"}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json({"error": f"Внутренняя ошибка: {exc}", "code": "server_error"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def secrets_compare(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="DemoExam Proxmox dashboard")
    parser.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8080")))
    parser.add_argument("--db", type=Path, default=DATA_ROOT / "dashboard.db")
    args = parser.parse_args()

    if not WEB_ROOT.exists():
        raise SystemExit(f"Frontend directory not found: {WEB_ROOT}")
    try:
        gateway = create_gateway()
    except Exception as exc:
        raise SystemExit(f"Не удалось инициализировать Proxmox: {exc}") from exc
    # Never insert synthetic VMIDs into a live inventory: a demo ID could
    # collide with a real guest and make lifecycle actions unsafe.
    store = DashboardStore(args.db.resolve(), seed_demo=gateway.mode == "demo")
    service = DashboardService(store, gateway)
    DashboardHandler.service = service
    DashboardHandler.admin_token = os.environ.get("DASHBOARD_ADMIN_TOKEN", "")
    mimetypes.add_type("application/javascript", ".js")
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"DemoExam Control Center: http://{args.host}:{args.port}")
    print(f"Режим Proxmox: {gateway.mode}")
    if not DashboardHandler.admin_token:
        print("Внимание: DASHBOARD_ADMIN_TOKEN не задан; оставляйте сервер только на localhost.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка сервера…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
