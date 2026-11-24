"""Standalone student preview. Normal startup never initializes the admin service or Proxmox."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from dashboard_backend.student_mount import StudentMount

ROOT = Path(__file__).resolve().parent


def handler_for(mount: StudentMount):
    class PreviewHandler(BaseHTTPRequestHandler):
        def dispatch(self):
            if mount.handle(self, self.command):
                return
            path = urlsplit(self.path).path
            if self.command in {"GET", "HEAD"} and path == "/":
                self.send_response(302)
                self.send_header("Location", "/student-demo/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.command in {"GET", "HEAD"} and path in ("/favicon.svg", "/favicon.ico"):
                content = (ROOT / "web" / path[1:]).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml" if path.endswith(".svg") else "image/x-icon")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                if self.command == "GET":
                    self.wfile.write(content)
            else:
                self.send_error(404)

        do_GET = do_HEAD = do_POST = do_PATCH = do_DELETE = dispatch

    return PreviewHandler


def main():
    parser = argparse.ArgumentParser(description="Изолированный демокабинет студента")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--db", type=Path, default=ROOT / "data/student-demo-state.db")
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/student-demo-catalog.json")
    parser.add_argument("--refresh-catalog", action="store_true", help="обновить список шаблонов и выйти")
    parser.add_argument("--source-db", type=Path, help="существующая база сценариев Deployer (только чтение)")
    args = parser.parse_args()
    if args.refresh_catalog:
        if not args.source_db:
            parser.error("--refresh-catalog требует --source-db")
        from dashboard_backend.student_catalog import refresh_catalog
        count = refresh_catalog(args.source_db, args.catalog)
        print(f"Шаблонов в каталоге: {count}")
        return
    if args.source_db:
        parser.error("--source-db используется только с --refresh-catalog")
    if args.db.resolve() == args.catalog.resolve():
        parser.error("Файлы базы и каталога должны быть разными")
    mount = StudentMount(args.db, args.catalog, secure_cookie=False)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(mount)) as server:
        print(f"http://127.0.0.1:{server.server_port}/student-demo/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
