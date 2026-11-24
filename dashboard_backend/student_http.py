"""HTTP adapter for the synthetic student portal. No admin API or PVE credentials."""
from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict, deque
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from .student_portal import StudentError


PREFIX = "/student-demo/api"
COOKIE = "deployer_student_demo"
BODY_LIMIT = 4096
RESOURCE_ID = r"[A-Za-z0-9_:-][A-Za-z0-9._:-]{0,127}"
SESSION_VALUE = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


class _LoginWindow:
    """Share a small failure budget per peer; valid sign-ins do not consume it."""
    def __init__(self, limit=30, seconds=60, max_peers=2048):
        self.limit, self.seconds, self.max_peers = limit, seconds, max_peers
        self.peers = OrderedDict()
        self.lock = threading.Lock()

    def reserve(self, peer):
        now = time.monotonic()
        with self.lock:
            attempts = self.peers.pop(peer, deque())
            while attempts and attempts[0] <= now - self.seconds:
                attempts.popleft()
            self.peers[peer] = attempts
            if len(self.peers) > self.max_peers:
                self.peers.popitem(last=False)
            if len(attempts) >= self.limit:
                return None
            attempts.append(now)
            return now

    def success(self, peer, reservation):
        with self.lock:
            attempts = self.peers.get(peer)
            if attempts and reservation in attempts:
                attempts.remove(reservation)
                if not attempts:
                    del self.peers[peer]


_logins = _LoginWindow()
_grants = _LoginWindow(limit=15)


def _header_values(headers, name):
    if hasattr(headers, "get_all"):
        return headers.get_all(name) or []
    value = headers.get(name)
    return [] if value is None else [value]


def _token(headers):
    lines = _header_values(headers, "Cookie")
    raw = "; ".join(lines)
    if len(raw) > 8192 or sum(part.strip().partition("=")[0] == COOKIE for part in raw.split(";")) != 1:
        return ""
    try:
        cookie = SimpleCookie(raw).get(COOKIE)
    except CookieError:
        return ""
    return cookie.value if cookie and SESSION_VALUE.fullmatch(cookie.value) else ""


def _cookie(token, secure):
    flags = "; Path=/student-demo/; HttpOnly; SameSite=Strict"
    return f"{COOKIE}={token}" + flags + ("; Secure" if secure else "") + ("" if token else "; Max-Age=0")


def _check_origin(headers, secure):
    origins = _header_values(headers, "Origin")
    fetch_sites = _header_values(headers, "Sec-Fetch-Site")
    if len(origins) > 1 or (fetch_sites and fetch_sites != ["same-origin"]):
        raise StudentError(403, "Запрос должен быть отправлен из этого кабинета.")
    if not origins:
        return  # SameSite cookie + JSON-only POST; we do not offer cross-origin CORS.
    try:
        hosts = _header_values(headers, "Host")
        scheme = "https" if secure else "http"
        origin = urlsplit(origins[0])
        dest = urlsplit(scheme + "://" + hosts[0]) if len(hosts) == 1 else None
        default = 443 if secure else 80
        if (not dest or not dest.hostname or dest.path or dest.query or dest.fragment or dest.username is not None
                or origin.scheme != scheme or origin.hostname != dest.hostname or origin.username is not None
                or origin.path or origin.query or origin.fragment
                or (default if origin.port is None else origin.port) != (default if dest.port is None else dest.port)):
            raise ValueError
    except ValueError:
        raise StudentError(403, "Запрос должен быть отправлен из этого кабинета.") from None


def _body(h):
    types = _header_values(h.headers, "Content-Type")
    if len(types) != 1 or types[0].partition(";")[0].strip().lower() != "application/json":
        raise StudentError(415, "Требуется запрос в формате JSON.")
    lengths = _header_values(h.headers, "Content-Length")
    if (h.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
            or not re.fullmatch(r"[0-9]{1,18}", lengths[0])):
        raise StudentError(400, "Некорректная длина запроса.")
    size = int(lengths[0])
    if size > BODY_LIMIT:
        raise StudentError(413, "Слишком большой запрос.")
    raw = h.rfile.read(size)
    try:
        if len(raw) != size:
            raise ValueError
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError
    except (ValueError, RecursionError):
        raise StudentError(400, "Ожидался JSON-объект.") from None
    return data


def _response(h, method, status, data, headers):
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    h.send_response(status)
    for name, value in (("Content-Type", "application/json; charset=utf-8"),
                        ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                        ("X-Content-Type-Options", "nosniff"), *headers):
        h.send_header(name, value)
    if method == "POST" and status >= 400:
        # Some rejections happen before reading the body.
        h.close_connection = True
        h.send_header("Connection", "close")
    h.end_headers()
    if method != "HEAD":
        h.wfile.write(body)


def dispatch_student(handler, method, portal, *, secure_cookie=False) -> bool:
    """Return False for other namespaces, including every administrative route."""
    path = handler.path.partition("?")[0]
    if path != PREFIX and not path.startswith(PREFIX + "/"):
        return False
    tail = path[len(PREFIX):]
    deploy = re.fullmatch(r"/assignments/(" + RESOURCE_ID + r")/deploy", tail)
    detail = re.fullmatch(r"/stands/(" + RESOURCE_ID + r")", tail)
    guest = re.fullmatch(r"/stands/(" + RESOURCE_ID + r")/(credentials|duration)", tail)
    allowed = "POST" if tail in {"/login", "/logout"} or deploy or guest else "GET" if tail == "/me" or detail else None
    headers = []
    status = 200
    try:
        if allowed is None:
            raise StudentError(404, "Страница кабинета не найдена.")
        if method != allowed:
            headers.append(("Allow", allowed))
            raise StudentError(405, "Этот метод не поддерживается.")
        if method == "POST":
            _check_origin(handler.headers, secure_cookie)
            data = _body(handler)
            if guest:
                fields = {"access_code", "duration_minutes"} if guest[2] == "duration" else {"access_code"}
                if set(data) != fields or not isinstance(data["access_code"], str) or not 1 <= len(data["access_code"]) <= 128:
                    raise StudentError(400, "Нужен персональный код доступа этой VM и корректные параметры")
            elif deploy and set(data) <= {"duration_minutes"}:
                pass
            elif tail != "/login" and data:
                raise StudentError(400, "Этой операции не нужны дополнительные параметры.")
        token = _token(handler.headers)
        if tail == "/login":
            if (set(data) != {"login", "password"} or not isinstance(data["login"], str)
                    or not isinstance(data["password"], str) or len(data["login"]) > 80 or len(data["password"]) > 256):
                raise StudentError(400, "Введите демологин и пароль.")
            peer = handler.client_address[0]
            slot = _logins.reserve(peer)
            if slot is None:
                headers.append(("Retry-After", str(_logins.seconds)))
                raise StudentError(429, "Слишком много попыток входа. Попробуйте через минуту.")
            new_token = portal.login(data["login"], data["password"])
            _logins.success(peer, slot)
            if not isinstance(new_token, str) or not SESSION_VALUE.fullmatch(new_token):
                raise RuntimeError("Invalid generated demo session")
            if token and token != new_token:
                portal.logout(token)
            headers.append(("Set-Cookie", _cookie(new_token, secure_cookie)))
            result = {"ok": True}
        elif tail == "/logout":
            if token:
                portal.logout(token)
            headers.append(("Set-Cookie", _cookie("", secure_cookie)))
            result = {"ok": True}
        elif not token:
            raise StudentError(401, "Войдите в демоучётную запись.")
        elif tail == "/me":
            result = portal.get_dashboard(token)
        elif detail:
            result = portal.get_stand(token, detail[1])
        elif guest:
            peer = handler.client_address[0]
            slot = _grants.reserve(peer)
            if slot is None:
                headers.append(("Retry-After", str(_grants.seconds)))
                raise StudentError(429, "Слишком много попыток. Попробуйте через минуту.")
            result = portal.guest_action(token, guest[1], guest[2], data)
            _grants.success(peer, slot)
        else:
            result = {"stand": portal.deploy(token, deploy[1], **data)}
    except StudentError as exc:
        status, result = exc.status, {"error": str(exc)}
    except Exception:
        status, result = 500, {"error": "Кабинет временно недоступен. Попробуйте ещё раз."}
    _response(handler, method, status, result, headers)
    return True
