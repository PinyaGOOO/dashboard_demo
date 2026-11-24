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

from dashboard_backend.consumer_monitor import ConsumerReportUnavailable
from dashboard_backend.database import DashboardStore
from dashboard_backend.proxmox_gateway import create_gateway
from dashboard_backend.service import ConflictError, DashboardService, NotFoundError, ValidationError
from dashboard_backend.student_mount import StudentMount
from dashboard_backend.student_credentials import PilotCredentials
from dashboard_backend.student_portal import StudentError
from dashboard_backend.student_pilot_validation import make_pilot_validator


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DATA_ROOT = ROOT / "data"
MAX_BODY = 2 * 1024 * 1024


class DashboardHandler(SimpleHTTPRequestHandler):
    server_version = "DemoExamDashboard/1.0"
    service: DashboardService
    admin_token: str
    student_mount: StudentMount | None = None
    pilot_grant: PilotCredentials | None = None

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, message: str, *args: Any) -> None:
        sys.stdout.write(f"[dashboard] {self.address_string()} - {message % args}\n")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if not self.path.startswith("/student-demo/") and self.path.split("?", 1)[0] != "/student-demo":
            self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        super().end_headers()

    def do_GET(self) -> None:
        if self._student("GET"):
            return
        if self.path.startswith("/api/"):
            self._handle_api("GET")
            return
        parsed = urlparse(self.path)
        if parsed.path != "/" and not (WEB_ROOT / parsed.path.lstrip("/")).is_file():
            self.path = "/index.html"
        super().do_GET()

    def _student(self, method: str) -> bool:
        return self.student_mount is not None and self.student_mount.handle(self, method)

    def do_HEAD(self) -> None:
        if not self._student("HEAD"):
            super().do_HEAD()

    def do_POST(self) -> None:
        if not self._student("POST"):
            self._handle_api("POST")

    def do_PATCH(self) -> None:
        if not self._student("PATCH"):
            self._handle_api("PATCH")

    def do_DELETE(self) -> None:
        if not self._student("DELETE"):
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
        sensitive = method == "GET" and (
            (bool(parts) and parts[-1] in {"credentials", "relief-candidates"})
            or parts in (["api", "web-activity"], ["api", "resource-consumers"])
        )
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
            elif parts == ["api", "resource-consumers"] and method == "GET":
                self._json(self.service.resource_consumers())
            elif parts == ["api", "operation-queue"] and method == "GET":
                self._json(self.service.operation_queue())
            elif parts == ["api", "web-activity"] and method == "GET":
                force = query.get("force", ["0"])[0] in {"1", "true", "yes"}
                self._json(self.service.web_activity(force=force))
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
                self._json(self.service.list_stands(include_pool_inventory=True))
            elif parts == ["api", "stands"] and method == "POST":
                self._json(self.service.create_stand(self._body()), HTTPStatus.ACCEPTED)
            elif parts == ["api", "stands", "actions"] and method == "POST":
                body = self._body()
                if str(body.get("action", "")) != "rollback_start_all":
                    raise ValidationError("Неизвестное массовое действие")
                result = (
                    self.service.rollback_all_stands(body["workspace"])
                    if body.get("workspace") is not None
                    else self.service.rollback_all_stands()
                )
                self._json(result, HTTPStatus.ACCEPTED)
            elif parts == ["api", "pools"] and method == "GET":
                self._json(self.service.list_pools())
            elif parts == ["api", "ipam"] and method == "GET":
                self._json(self.service.list_ipam())
            elif parts == ["api", "pools", "import"] and method == "POST":
                self._json(self.service.import_pool(self._body()), HTTPStatus.CREATED)
            elif len(parts) == 3 and parts[:2] == ["api", "stands"] and method == "GET":
                self._json(self.service.get_stand(int(parts[2]), include_pool_inventory=True))
            elif len(parts) == 3 and parts[:2] == ["api", "stands"] and method == "PATCH":
                self._json(self.service.update_stand(int(parts[2]), self._body()))
            elif len(parts) == 3 and parts[:2] == ["api", "stands"] and method == "DELETE":
                self.service.delete_stand(int(parts[2])); self._json({"ok": True})
            elif len(parts) == 4 and parts[:2] == ["api", "stands"] and parts[3] == "actions" and method == "POST":
                body = self._body()
                action = str(body.get("action", ""))
                status = HTTPStatus.ACCEPTED if action in {"rollback_start", "retry_deploy", "snapshot"} else HTTPStatus.OK
                self._json(self.service.stand_action(int(parts[2]), action, body), status)
            elif len(parts) == 4 and parts[:2] == ["api", "stands"] and parts[3] == "relief-candidates" and method == "GET":
                self._json(self.service.relief_candidates(int(parts[2])))
            elif len(parts) == 4 and parts[:2] == ["api", "stands"] and parts[3] == "credentials" and method == "GET":
                result = self.service.stand_credentials(int(parts[2]))
                if self.pilot_grant:
                    for vm in result["credentials"]:
                        code = self.pilot_grant.operator_code(int(parts[2]), vm.get("vmid"))
                        if code:
                            vm["student_access_code"] = code
                self._json(result)
            elif len(parts) == 6 and parts[:2] == ["api", "stands"] and parts[3] == "vms" and parts[5] == "actions" and method == "POST":
                body = self._body()
                action = str(body.get("action", ""))
                status = HTTPStatus.ACCEPTED if action in {"run_check", "rollback_start", "snapshot"} else HTTPStatus.OK
                self._json(self.service.vm_action(int(parts[2]), int(parts[4]), action, body), status)
            elif len(parts) == 6 and parts[:2] == ["api", "stands"] and parts[3] == "vms" and parts[5] == "credentials" and method == "GET":
                result = self.service.vm_credentials(int(parts[2]), int(parts[4]))
                code = self.pilot_grant.operator_code(int(parts[2]), int(parts[4])) if self.pilot_grant else None
                if code:
                    result["student_access_code"] = code
                self._json(result)
            elif parts == ["api", "checks"] and method == "GET":
                self._json(self.service.list_checks())
            elif parts == ["api", "checks", "run"] and method == "POST":
                body = self._body(); self._json(self.service.start_check(int(body.get("stand_id"))), HTTPStatus.ACCEPTED)
            else:
                self._json({"error": "Маршрут API не найден", "code": "not_found"}, HTTPStatus.NOT_FOUND)
        except ConsumerReportUnavailable as exc:
            self._json({"error": str(exc), "code": "telemetry_unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
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
    admin_db = args.db.resolve()
    student_db = admin_db.with_name("student-demo-state.db")
    if admin_db == student_db.resolve():
        parser.error("Для административной базы и студенческого демо нужны разные файлы")
    store = DashboardStore(admin_db, seed_demo=gateway.mode == "demo")
    service = DashboardService(store, gateway)
    DashboardHandler.service = service
    DashboardHandler.admin_token = os.environ.get("DASHBOARD_ADMIN_TOKEN", "")
    grant = PilotCredentials(admin_db, DashboardHandler.admin_token) if gateway.mode == "live" and DashboardHandler.admin_token else None
    DashboardHandler.pilot_grant = grant

    def set_student_duration(binding, minutes):
        try:
            return service.set_stand_duration(binding["deployer_stand_id"], minutes,
                expected_vmid=binding["vmid"], expected_pool=binding["pool_id"], expected_ip=binding["guest_ipv4"])
        except (ConflictError, ValidationError, NotFoundError):
            raise StudentError(409, "Не удалось назначить срок: проверьте состояние стенда и наличие активного таймера") from None

    DashboardHandler.student_mount = StudentMount(
        student_db, admin_db.with_name("student-demo-catalog.json"),
        secure_cookie=gateway.mode == "live", example_if_missing=gateway.mode == "demo",
        pilot_validator=make_pilot_validator(admin_db) if gateway.mode == "live" else None,
        pilot_grant=grant, set_duration=set_student_duration if grant else None,
    )
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
        service.stop_monitors()
        server.server_close()


if __name__ == "__main__":
    main()
