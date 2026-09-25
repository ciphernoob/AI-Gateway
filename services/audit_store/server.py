import argparse
import hmac
import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .store import AuditError, Store, require


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            try:
                with self.server.store.transaction():
                    self.server.store.db.execute("CREATE TABLE IF NOT EXISTS readiness(id INTEGER)")
                    self.server.store.db.execute("DELETE FROM readiness")
                    self.server.store.db.execute("INSERT INTO readiness VALUES(1)")
                    rows = self.server.store.db.execute("SELECT capture_state,COUNT(*) FROM requests WHERE expires>? GROUP BY capture_state",
                                                        (self.server.store.clock(),)).fetchall()
                    states = dict(rows)
                return self.respond(200, {"ok": True, "pending_requests": states.get('pending', 0),
                                         "incomplete_requests": states.get('incomplete', 0)})
            except sqlite3.Error:
                return self.respond(503, {"error": {"code": "audit_unavailable"}})
        self.respond(404, {"error": {"code": "not_found"}})

    def do_POST(self):
        store = self.server.store
        try:
            auth = self.headers.get("Authorization", "")
            if self.path in {'/admin/query', '/admin/redaction'}:
                token_path = os.environ.get('ADMIN_INTERNAL_TOKEN_FILE')
                token = Path(token_path).read_text().strip() if token_path else ''
                require(bool(token) and hmac.compare_digest(auth, 'Bearer ' + token), 401, 'invalid_admin_service_key')
                size = int(self.headers.get('Content-Length', '0'))
                require(0 < size <= 16 * 1024 * 1024, 413, 'body_too_large')
                data = json.loads(self.rfile.read(size))
                result = store.sync_admin_redaction(data.get('revision')) if self.path == '/admin/redaction' else store.admin_query(data)
                return self.respond(200, result)
            require(hmac.compare_digest(auth, "Bearer " + store.policy["token"]), 401, "invalid_service_key")
            size = int(self.headers.get("Content-Length", "0"))
            require(0 < size <= 16 * 1024 * 1024, 413, "body_too_large")
            body = json.loads(self.rfile.read(size))
            identity, data = body["identity"], body["data"]
            require(isinstance(identity, dict) and isinstance(identity.get("user_id"), str) and isinstance(data, dict))
            route = self.path
            if route == "/internal/tool":
                require("audit:write" in identity.get("scopes", []), 403, "insufficient_scope")
                result, status = store.tool_event(identity, data)
            elif route == "/internal/query":
                require("audit:read" in identity.get("scopes", []), 403, "insufficient_scope")
                params = data.get("params", {})
                resource = data["resource"]
                if resource == "requests":
                    result = store.list_requests(identity["user_id"], params)
                elif resource == "request":
                    result = store.detail(identity["user_id"], data["id"], params)
                elif resource == "trace":
                    result = store.list_requests(identity["user_id"], params, data["id"])
                else:
                    raise AuditError(404, "not_found")
                status = 200
            else:
                method = {"/internal/prepare": store.prepare, "/internal/attempt": store.attempt,
                          "/internal/finish": store.finish, "/internal/usage": store.link_usage,
                          "/internal/reconcile": store.reconcile}.get(route)
                require(method is not None, 404, "not_found")
                result, status = method(identity, data), 200
            self.respond(status, result)
        except AuditError as exc:
            self.close_connection = True
            if self.path == "/internal/query" and "identity" in locals():
                try:
                    with store.transaction():
                        store.record_read(identity["user_id"], store.redact(str(data.get("id", "requests")))[:200], exc.status)
                except sqlite3.Error:
                    return self.respond(503, {"error": {"code": "audit_unavailable"}})
            self.respond(exc.status, {"error": {"code": exc.code, "message": exc.code, "type": "audit_error"}})
        except (ValueError, KeyError, TypeError, RecursionError):
            self.close_connection = True
            self.respond(400, {"error": {"code": "invalid_event"}})
        except (sqlite3.Error, OSError):
            self.respond(503, {"error": {"code": "audit_unavailable"}})


def make_server(store, host="0.0.0.0", port=8001):
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    Path(args.database).parent.mkdir(parents=True, exist_ok=True)
    store = Store(args.database, config)
    server = make_server(store, port=args.port)
    stop = threading.Event()

    def maintenance():
        while not stop.wait(1):
            try:
                store.maintain()
            except sqlite3.Error:
                print('{"event":"audit_maintenance_failed"}', flush=True)

    threading.Thread(target=maintenance, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
