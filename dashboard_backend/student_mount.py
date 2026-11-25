"""Mount the self-contained student demo in either Deployer or the standalone server."""
from collections.abc import Callable
from pathlib import Path
from sqlite3 import Error as SQLiteError
from threading import Lock

from .student_catalog import demo_assignments, load_catalog
from .student_http import dispatch_student
from .student_portal import StudentError, StudentPortal


_PREFIX = "/student-demo"
_ROOT = Path(__file__).resolve().parents[1] / "web" / "student-demo"
_ASSETS = {
    "/": ("index.html", "text/html"),
    "/index.html": ("index.html", "text/html"),
    "/student.js": ("student.js", "text/javascript"),
    "/assignment-markdown.js": ("assignment-markdown.js", "text/javascript"),
    "/student.css": ("student.css", "text/css"),
}


def _reply(h, method, status, body=b"", mime="text/plain", headers=()):
    h.send_response(status)
    h.send_header("Content-Type", f"{mime}; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-cache" if status == 200 else "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Referrer-Policy", "same-origin")
    for name, value in headers:
        h.send_header(name, value)
    if method not in ("GET", "HEAD"):
        h.close_connection = True
        h.send_header("Connection", "close")
    h.end_headers()
    if method != "HEAD" and body:
        h.wfile.write(body)


class _LazyPortal:
    """Initialize the portal only for recognized requests that need it."""

    def __init__(self, db_path, catalog_path, example_if_missing, pilot_validator, pilot_grant, set_duration, labs,
                 published_catalog):
        self.db_path = Path(db_path)
        self.catalog_path = Path(catalog_path)
        self.example_if_missing = example_if_missing
        self.pilot_validator = pilot_validator
        self.pilot_grant = pilot_grant
        self.set_duration = set_duration
        self.labs = labs
        self.published_catalog = published_catalog
        self.instance = None
        self.lock = Lock()

    def __getattr__(self, name):
        if name not in {"login", "logout", "get_dashboard", "deploy", "get_stand", "guest_action", "demo_profiles"}:
            raise AttributeError(name)
        return getattr(self._initialized(), name)

    def _initialized(self):
        with self.lock:
            if self.instance is None:
                try:
                    if self.db_path.resolve() == self.catalog_path.resolve():
                        raise ValueError("Demo database and catalog must be different")
                    examples = demo_assignments if self.published_catalog is not None else (
                        lambda: load_catalog(self.catalog_path, example_if_missing=self.example_if_missing))
                    self.instance = StudentPortal(self.db_path, examples,
                        pilot_validator=self.pilot_validator, pilot_grant=self.pilot_grant, set_duration=self.set_duration,
                        labs=self.labs, published_catalog=self.published_catalog)
                except (OSError, RuntimeError, SQLiteError, ValueError) as exc:
                    raise StudentError(503, "Хранилище демокабинета недоступно.") from exc
            return self.instance


class StudentMount:
    def __init__(self, db_path: Path, catalog_path: Path, *, secure_cookie: bool,
                 example_if_missing: bool = True, pilot_validator: Callable[[dict], bool] | None = None,
                 pilot_grant=None, set_duration=None, labs=None, published_catalog=None):
        self._portal = _LazyPortal(db_path, catalog_path, example_if_missing, pilot_validator, pilot_grant, set_duration,
                                   labs, published_catalog)
        self._secure_cookie = secure_cookie

    def create_accounts(self, payload):
        """Called by the separate authenticated operator endpoint; not a student action."""
        return self._portal._initialized().create_accounts(payload)

    def handle(self, handler, method) -> bool:
        path = handler.path.partition("?")[0]
        # Claim ambiguous encoded separators, but never decode them into an asset path.
        invalid = path.lower().startswith((_PREFIX + "%2f", _PREFIX + "%5c", _PREFIX + "\\"))
        if path != _PREFIX and not path.startswith(_PREFIX + "/") and not invalid:
            return False
        if dispatch_student(handler, method, self._portal, secure_cookie=self._secure_cookie):
            return True
        asset = _ASSETS.get(path[len(_PREFIX):])
        if path != _PREFIX and asset is None:
            _reply(handler, method, 404, "Страница демокабинета не найдена.".encode())
        elif method not in ("GET", "HEAD"):
            _reply(handler, method, 405, "Этот метод не поддерживается.".encode(),
                   headers=(("Allow", "GET, HEAD"),))
        elif path == _PREFIX:
            _reply(handler, method, 308, headers=(("Location", _PREFIX + "/"),))
        else:
            filename, mime = asset
            try:
                data = (_ROOT / filename).read_bytes()
            except OSError:
                _reply(handler, method, 503, "Файлы демокабинета недоступны.".encode())
            else:
                _reply(handler, method, 200, data, mime)
        return True
