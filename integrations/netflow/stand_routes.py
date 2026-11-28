"""HTTP adapter for the Netflow WSGI app. No web server or student login of its own."""
import hmac
import json
import os
import re
from functools import lru_cache

if __package__:
    from .stand_queue import QueueError, StandQueue
else:  # installed alongside Netflow's app.py
    from stand_queue import QueueError, StandQueue


STAND = "/api/me/stand"
SCENARIOS = STAND + "/scenarios"
CLAIM = "/api/integration/jobs/claim"
STATUS = "/api/integration/jobs/status"
STUDENT_PATHS = {STAND, SCENARIOS}
WORKER_PATHS = {CLAIM, STATUS}
_TOKEN = re.compile(r"[!-~]{32,256}", re.ASCII)


class RouteError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def _enabled():
    if os.environ.get("NETFLOW_STANDS_ENABLED", "0") != "1":
        raise RouteError(404, "Сервис стендов выключен.")


def _settings_token():
    token = os.environ.get("NETFLOW_WORKER_TOKEN", "")
    if not _TOKEN.fullmatch(token):
        raise RouteError(503, "Сервис стендов не настроен.")
    return token


def _catalog():
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = os.environ.get("NETFLOW_STAND_SCENARIOS", "")
        if len(value) > 65536:
            raise ValueError
        parsed = json.loads(value, object_pairs_hook=unique)
        if not isinstance(parsed, dict) or not parsed or len(parsed) > 512:
            raise ValueError
        for key, groups in parsed.items():
            if (not re.fullmatch(r"lab-[1-9][0-9]{0,9}-[1-9][0-9]{0,8}", key)
                    or not isinstance(groups, list) or not 1 <= len(groups) <= 256
                    or any(not isinstance(x, str) or not x.strip() or not x.isprintable()
                           or x != x.strip() or len(x) > 60 for x in groups)
                    or len({x.casefold() for x in groups}) != len(groups)):
                raise ValueError
        return parsed
    except (TypeError, ValueError, RecursionError):
        raise RouteError(503, "Не настроены разрешённые сценарии стендов.") from None


@lru_cache(maxsize=1)
def _configured(db):
    return StandQueue(db, _catalog())


def prepare(db):
    """Initialize extension tables outside Netflow's BEGIN IMMEDIATE."""
    _enabled()
    _settings_token()
    return _configured(db)


def student(db, conn, uid, environ, path, data):
    """Only call AFTER Netflow's active-session, origin, CSRF and must_change checks."""
    if path not in STUDENT_PATHS:
        raise RouteError(404, "Маршрут не найден.")
    queue = prepare(db).using(conn)
    method = environ.get("REQUEST_METHOD", "GET")
    if method == "GET" and not data:
        return queue.scenarios(uid) if path == SCENARIOS else queue.get(uid)
    if method == "POST" and path == STAND:
        if not isinstance(data, dict) or set(data) != {"assignment_id"} or not isinstance(data["assignment_id"], str):
            raise RouteError(400, "Нужен только идентификатор сценария.")
        return queue.request(uid, data["assignment_id"])
    raise RouteError(405, "Метод не поддерживается.")


def worker(db, environ, path, data):
    """Narrow outbound-worker API. Never interprets browser cookies as authority."""
    _enabled()
    if path not in WORKER_PATHS:
        raise RouteError(404, "Маршрут не найден.")
    if environ.get("REQUEST_METHOD") != "POST":
        raise RouteError(405, "Метод не поддерживается.")
    if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
        raise RouteError(415, "Ожидается JSON.")
    if environ.get("HTTP_COOKIE") or environ.get("HTTP_ORIGIN"):
        raise RouteError(403, "Этот маршрут предназначен только для Deployer.")
    token = _settings_token()
    auth = environ.get("HTTP_AUTHORIZATION", "")
    if not isinstance(auth, str) or not auth.isascii() or not hmac.compare_digest(auth, "Bearer " + token):
        raise RouteError(401, "Нужен служебный ключ.")
    if not isinstance(data, dict):
        raise RouteError(400, "Ожидается объект JSON.")
    if path == CLAIM:
        if data:
            raise RouteError(400, "Для получения задачи нужен пустой объект.")
        return _configured(db).claim()
    required = {"job_id", "lease_token", "status", "progress"}
    if not (required <= data.keys() <= required | {"web_url"}):
        raise RouteError(400, "Некорректные поля статуса.")
    return _configured(db).report(**data)
