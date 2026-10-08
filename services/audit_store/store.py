"""User-scoped, append-only audit events with separately expiring content."""
import base64
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class AuditError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def require(condition, status=400, code="invalid_event"):
    if not condition:
        raise AuditError(status, code)


class Store:
    def __init__(self, path, config, clock=time.time, revision_dir=None):
        self.config, self.policy, self.clock = config, config["audit"], clock
        self.revision_dir = Path(revision_dir or os.environ.get('AUDIT_REVISION_DIR', '/runtime/revisions'))
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS traces(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, policy TEXT NOT NULL,
            sampled INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            trace_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, capture_state TEXT NOT NULL,
            created REAL NOT NULL, deadline REAL NOT NULL, expires REAL NOT NULL, meta TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS requests_owner_time ON requests(user_id,created,id);
          CREATE INDEX IF NOT EXISTS requests_trace ON requests(user_id,trace_id,created,id);
          CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, request_id TEXT NOT NULL, status TEXT NOT NULL,
            meta TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS payloads(id TEXT PRIMARY KEY, data TEXT, digest TEXT NOT NULL, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            agent_id TEXT NOT NULL, event_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt_id TEXT,
            source TEXT NOT NULL, type TEXT NOT NULL, received REAL NOT NULL, occurred TEXT, meta TEXT NOT NULL,
            fingerprint TEXT NOT NULL, payload_id TEXT, payload_state TEXT NOT NULL, expires REAL NOT NULL,
            UNIQUE(user_id,agent_id,event_id));
          CREATE INDEX IF NOT EXISTS events_request ON events(user_id,request_id,seq);
          CREATE TABLE IF NOT EXISTS tool_calls(attempt_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
            request_id TEXT NOT NULL, name TEXT, PRIMARY KEY(attempt_id,tool_call_id));
          CREATE TABLE IF NOT EXISTS executions(user_id TEXT NOT NULL, agent_id TEXT NOT NULL, execution_id TEXT NOT NULL,
            request_id TEXT NOT NULL, attempt_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
            start_event TEXT, terminal_event TEXT, terminal_type TEXT,
            PRIMARY KEY(user_id,agent_id,execution_id));
          CREATE TABLE IF NOT EXISTS reads(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, target TEXT NOT NULL,
            received REAL NOT NULL, result INTEGER NOT NULL, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS admin_redaction_revisions(revision TEXT PRIMARY KEY, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS admin_reads(id TEXT PRIMARY KEY, actor TEXT NOT NULL, target TEXT NOT NULL,
            received REAL NOT NULL, result INTEGER NOT NULL);
        """)
        # Upgrade the pre-release implementation: preserve references to private
        # snapshots, then erase its duplicated plaintext credential table.
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='admin_redaction'").fetchone():
            legacy = dict(self.db.execute('SELECT value,expires FROM admin_redaction'))
            for entry in self.revision_dir.glob('*.json'):
                values = self.revision_secrets(entry.stem)
                expiry = max((legacy.get(value, 0) for value in values), default=0)
                if expiry:
                    self.db.execute('INSERT OR REPLACE INTO admin_redaction_revisions VALUES(?,?)', (entry.stem, expiry))
            self.db.execute('PRAGMA secure_delete=ON')
            self.db.execute('DROP TABLE admin_redaction')
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.refresh_admin_redaction()

    def revision_secrets(self, revision):
        require(isinstance(revision, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', revision), code='invalid_revision')
        value = json.loads((self.revision_dir / (revision + '.json')).read_text())
        require(value.get('revision') == revision, code='invalid_revision')
        values = value.get('known_secrets')
        require(isinstance(values, list) and len(values) <= 10000)
        require(all(isinstance(v, str) and 16 <= len(v) <= 4096 for v in values))
        return values

    def refresh_admin_redaction(self):
        # Only version references live in SQLite. Plaintext stays in the existing
        # private runtime snapshots and this process's redaction memory.
        values = set()
        for row in self.db.execute('SELECT revision FROM admin_redaction_revisions WHERE expires>?', (self.clock(),)):
            values.update(self.revision_secrets(row[0]))
        self.admin_secrets = sorted(values, key=len, reverse=True)

    def sync_admin_redaction(self, revision):
        self.revision_secrets(revision)  # Fail before acknowledging a missing snapshot.
        with self.transaction():
            expires = self.clock() + self.policy['content_retention_days'] * 86400 + 86400
            # Previous active credentials enter retirement; currently active ones
            # remain redacted indefinitely, including without another publication.
            self.db.execute('UPDATE admin_redaction_revisions SET expires=? WHERE expires>?', (expires, expires))
            self.db.execute('INSERT INTO admin_redaction_revisions VALUES(?,?) ON CONFLICT(revision) DO UPDATE SET expires=excluded.expires', (revision, 253402300799))
            self.refresh_admin_redaction()
        return {"ok": True}

    def admin_query(self, data):
        resource = data.get('resource')
        params = data.get('params', {})
        require(isinstance(params, dict))
        if resource == 'requests':
            require(not set(params) - {'user_id', 'model', 'status', 'from', 'to', 'offset', 'limit', 'request_id'})
            try:
                limit, offset = int(params.get('limit', 50)), int(params.get('offset', 0))
                start, end = float(params.get('from') or 0), float(params.get('to') or self.clock())
                require(1 <= limit <= 100 and 0 <= offset <= 1000000 and math.isfinite(start) and math.isfinite(end) and start <= end)
            except (TypeError, ValueError):
                raise AuditError(400, 'invalid_query')
            sql = 'SELECT id,user_id,agent_id,trace_id,model,status,capture_state,created FROM requests WHERE expires>? AND created>=? AND created<=?'
            args = [self.clock(), start, end]
            for field, key in [('user_id','user_id'), ('model','model'), ('status','status'), ('id','request_id')]:
                if params.get(key):
                    sql += ' AND ' + field + '=?'
                    args.append(params[key])
            with self.transaction():
                rows = self.db.execute(sql + ' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?', args + [limit + 1, offset]).fetchall()
                self.record_admin_read('requests', 200)
            return {'data': [dict(r) for r in rows[:limit]], 'has_more': len(rows) > limit}
        require(resource in {'request', 'trace'}, 404, 'not_found')
        rid = data.get('id', '')
        require(isinstance(rid, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', rid), code='invalid_id')
        with self.transaction():
            table = 'requests' if resource == 'request' else 'traces'
            row = self.db.execute('SELECT user_id FROM ' + table + ' WHERE id=? AND expires>?', (rid, self.clock())).fetchone()
            self.record_admin_read(rid, 200 if row else 404)
        require(row is not None, 404, 'not_found')
        return self.detail(row['user_id'], rid, params) if resource == 'request' else self.list_requests(row['user_id'], params, rid)

    def record_admin_read(self, target, status):
        self.db.execute('INSERT INTO admin_reads VALUES(?,?,?,?,?)', (uuid.uuid4().hex, 'admin', target, self.clock(), status))

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def redact(self, value):
        names = {x.lower() for x in self.policy["redact_fields"]} | {"authorization", "cookie", "set-cookie"}
        secrets = self.config["known_secrets"] + self.admin_secrets

        def walk(v, depth=0):
            require(depth <= 64, code="content_too_deep")
            if isinstance(v, dict):
                out = {}
                for key, child in v.items():
                    require(isinstance(key, str))
                    safe_key = walk(key, depth + 1)
                    if key.lower() in names:
                        out[safe_key] = "[REDACTED]"
                    elif key == "arguments" and isinstance(child, str):
                        try:
                            parsed = json.loads(child)
                        except (ValueError, RecursionError):
                            out[safe_key] = "[NOT_CAPTURED: incomplete arguments]"
                        else:
                            out[safe_key] = canonical(walk(parsed, depth + 1))
                    else:
                        out[safe_key] = walk(child, depth + 1)
                return out
            if isinstance(v, list):
                return [walk(x, depth + 1) for x in v]
            if isinstance(v, str):
                for secret in secrets:
                    v = v.replace(secret, "[REDACTED]")
                return v
            return v
        return walk(value)

    def owned_request(self, user, request_id):
        row = self.db.execute("SELECT * FROM requests WHERE id=? AND user_id=? AND expires>?",
                              (request_id, user, self.clock())).fetchone()
        require(row is not None, 404, "not_found")
        return row

    def trace_policy(self, trace_id):
        row = self.db.execute("SELECT * FROM traces WHERE id=?", (trace_id,)).fetchone()
        return json.loads(row["policy"]), bool(row["sampled"])

    def append(self, identity, request, event_id, kind, meta, payload=None, source="gateway_observed", attempt_id=None, occurred=None, payload_state=None):
        """Caller owns the transaction. Redaction precedes fingerprinting and insertion."""
        now = self.clock()
        policy, sampled = self.trace_policy(request["trace_id"])
        safe_meta = self.redact(meta)
        safe_payload = self.redact(payload) if payload is not None else None
        fingerprint = hashlib.sha256(canonical([kind, source, request["id"], attempt_id, occurred, safe_meta, safe_payload]).encode()).hexdigest()
        old = self.db.execute("SELECT * FROM events WHERE user_id=? AND agent_id=? AND event_id=?",
                              (identity["user_id"], identity.get("agent_id") or "", event_id)).fetchone()
        if old:
            require(old["fingerprint"] == fingerprint, 409, "event_conflict")
            return old["seq"], False
        state, payload_id = payload_state or "not_captured", None
        if safe_payload is not None and policy["mode"] == "full" and sampled:
            encoded = canonical(safe_payload)
            maximum = policy["response_max_bytes_per_attempt"] if kind == "attempt.finished" else policy["request_max_bytes"]
            expiry = min(request["created"] + policy["content_retention_days"] * 86400, request["expires"])
            if now >= expiry:
                state = "expired"
            elif len(encoded.encode()) > maximum:
                state = "truncated"
            else:
                payload_id = uuid.uuid4().hex
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                self.db.execute("INSERT INTO payloads VALUES(?,?,?,?)", (payload_id, encoded, digest, expiry))
                state = payload_state or ("redacted" if safe_payload != payload else "captured")
        cursor = self.db.execute("""INSERT INTO events(user_id,agent_id,event_id,request_id,attempt_id,source,type,
            received,occurred,meta,fingerprint,payload_id,payload_state,expires) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (identity["user_id"], identity.get("agent_id") or "", event_id, request["id"], attempt_id, source, kind,
             now, occurred, canonical(safe_meta), fingerprint, payload_id, state, request["expires"]))
        return cursor.lastrowid, True

    def prepare(self, identity, data):
        now = self.clock()
        with self.transaction():
            trace = data["trace_id"]
            if data.get("trace_provided"):
                found = self.db.execute("SELECT id FROM traces WHERE id=? AND user_id=? AND expires>?",
                                        (trace, identity["user_id"], now)).fetchone()
                require(found is not None, 404, "not_found")
            else:
                policy = {k: v for k, v in self.policy.items() if k not in {"token", "cursor_secret"}}
                sampled = int(int(hashlib.sha256(trace.encode()).hexdigest()[:13], 16) / 16**13 < policy["sample_rate"])
                self.db.execute("INSERT OR IGNORE INTO traces VALUES(?,?,?,?,?,?)",
                                (trace, identity["user_id"], canonical(policy), sampled, now, now + policy["metadata_retention_days"] * 86400))
                owner = self.db.execute("SELECT user_id FROM traces WHERE id=?", (trace,)).fetchone()
                require(owner["user_id"] == identity["user_id"], 404, "not_found")
            if data.get("parent_request_id"):
                parent = self.owned_request(identity["user_id"], data["parent_request_id"])
                require(parent["trace_id"] == trace, 400, "trace_parent_mismatch")
            policy, sampled = self.trace_policy(trace)
            expiry = now + policy["metadata_retention_days"] * 86400
            self.db.execute("UPDATE traces SET expires=MAX(expires,?) WHERE id=?", (expiry, trace))
            meta = self.redact({k: data.get(k) for k in ("parent_request_id", "conversation_id")})
            self.db.execute("INSERT OR IGNORE INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (data["request_id"], identity["user_id"], identity.get("agent_id") or "", trace,
                             data["requested_model"], "prepared", "pending", now, data["deadline"] + 10, expiry, canonical(meta)))
            row = self.owned_request(identity["user_id"], data["request_id"])
            self.append(identity, row, data["request_id"] + ":received", "request.received", meta, data.get("request"))
            return {"trace_id": trace, "sampled": sampled, "policy_version": policy["policy_version"]}

    def attempt(self, identity, data):
        with self.transaction():
            row = self.owned_request(identity["user_id"], data["request_id"])
            require(row["agent_id"] == (identity.get("agent_id") or ""), 404, "not_found")
            meta = {k: data.get(k) for k in ("provider", "model", "started_at")}
            self.append(identity, row, data["attempt_id"] + ":started", "attempt.started", meta, data.get("request"), attempt_id=data["attempt_id"])
            self.db.execute("INSERT OR IGNORE INTO attempts VALUES(?,?,?,?)", (data["attempt_id"], row["id"], "prepared", canonical(self.redact(meta))))
            return {"ok": True}

    def finish(self, identity, data):
        with self.transaction():
            row = self.owned_request(identity["user_id"], data["request_id"])
            attempt_id = data.get("attempt_id")
            if attempt_id:
                attempt = self.db.execute("SELECT * FROM attempts WHERE id=? AND request_id=?", (attempt_id, row["id"])).fetchone()
                require(attempt is not None, 404, "not_found")
                meta = {k: data.get(k) for k in ("status", "http_status", "usage", "usage_source", "capture_state", "captured_bytes", "received_bytes")}
                _, fresh = self.append(identity, row, attempt_id + ":finished", "attempt.finished", meta,
                                       data.get("response"), attempt_id=attempt_id, payload_state=data.get("payload_state"))
                if fresh:
                    self.db.execute("UPDATE attempts SET status=? WHERE id=?", (data["status"], attempt_id))
                    for tool in data.get("tool_calls", []):
                        require(isinstance(tool.get("id"), str) and len(tool["id"]) <= 200)
                        safe = self.redact({"id": tool["id"], "name": tool.get("name", "")})
                        self.db.execute("INSERT OR IGNORE INTO tool_calls VALUES(?,?,?,?)", (attempt_id, safe["id"], row["id"], safe["name"]))
                        self.append(identity, row, attempt_id + ":tool:" + safe["id"], "model.tool_call", safe, attempt_id=attempt_id)
            if data.get("final"):
                self.append(identity, row, row["id"] + ":finished", "request.finished",
                            {"status": data["status"], "http_status": data.get("http_status"), "final_attempt_id": attempt_id,
                             "capture_state": data.get("capture_state", "complete")})
                self.db.execute("UPDATE requests SET status=?,capture_state=? WHERE id=?",
                                (data["status"], data.get("capture_state", "complete"), row["id"]))
            return {"ok": True}

    def link_usage(self, identity, data):
        with self.transaction():
            row = self.owned_request(identity["user_id"], data["request_id"])
            self.append(identity, row, data["attempt_id"] + ":usage:" + str(data.get("version", 1)), "usage.linked",
                        {k: data.get(k) for k in ("usage", "usage_source", "version")}, attempt_id=data["attempt_id"])
        return {"ok": True}

    def tool_event(self, identity, event):
        require(identity.get("agent_id"), 403, "agent_required")
        require(not ({"user_id", "agent_id", "source"} & event.keys()))
        kind = event.get("type")
        require(kind in {"tool.started", "tool.completed", "tool.failed"})
        for key in ("event_id", "tool_execution_id", "request_id", "attempt_id", "tool_call_id", "trace_id"):
            require(isinstance(event.get(key), str) and re.fullmatch(r"[A-Za-z0-9_:.-]{1,200}", event[key]) is not None)
        try:
            occurred = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
            require(occurred.tzinfo is not None)
        except (ValueError, KeyError, AttributeError):
            raise AuditError(400, "invalid_time") from None
        require(kind != "tool.completed" or "result" in event)
        require(kind != "tool.failed" or isinstance(event.get("error"), dict))
        with self.transaction():
            row = self.owned_request(identity["user_id"], event["request_id"])
            require(row["agent_id"] == identity["agent_id"], 404, "not_found")
            require(row["trace_id"] == event["trace_id"], 400, "trace_parent_mismatch")
            require(self.clock() <= row["created"] + 7 * 86400, 410, "report_expired")
            call = self.db.execute("SELECT * FROM tool_calls WHERE request_id=? AND attempt_id=? AND tool_call_id=?",
                                   (row["id"], event["attempt_id"], event["tool_call_id"])).fetchone()
            if not call:
                raise AuditError(409 if row["capture_state"] == "pending" else 400,
                                 "audit_pending" if row["capture_state"] == "pending" else "unknown_tool_call")
            keys = (identity["user_id"], identity["agent_id"], event["tool_execution_id"])
            execution = self.db.execute("SELECT * FROM executions WHERE user_id=? AND agent_id=? AND execution_id=?", keys).fetchone()
            if execution:
                require((execution["request_id"], execution["attempt_id"], execution["tool_call_id"]) ==
                        (row["id"], event["attempt_id"], event["tool_call_id"]), 409, "execution_conflict")
                previous = execution["start_event"] if kind == "tool.started" else execution["terminal_event"]
                require(previous is None or previous == event["event_id"], 409, "execution_conflict")
            meta = {k: event[k] for k in ("tool_execution_id", "tool_call_id", "trace_id")}
            meta["name"] = call["name"]
            payload = {k: event[k] for k in ("arguments", "result", "error") if k in event}
            seq, fresh = self.append(identity, row, event["event_id"], kind, meta, payload,
                                     "agent_reported", event["attempt_id"], event["occurred_at"])
            if fresh:
                if not execution:
                    self.db.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?)", keys +
                                    (row["id"], event["attempt_id"], event["tool_call_id"], None, None, None))
                if kind == "tool.started":
                    self.db.execute("UPDATE executions SET start_event=? WHERE user_id=? AND agent_id=? AND execution_id=?", (event["event_id"],) + keys)
                else:
                    self.db.execute("UPDATE executions SET terminal_event=?,terminal_type=? WHERE user_id=? AND agent_id=? AND execution_id=?", (event["event_id"], kind) + keys)
            return {"event_id": event["event_id"], "ingest_seq": seq, "persisted": True}, 201 if fresh else 200

    def cursor(self, user, filters, position=None, token=None):
        key = self.policy["cursor_secret"].encode()
        if token is not None:
            try:
                raw = base64.urlsafe_b64decode(token.encode())
                signature, body = raw[:32], raw[32:]
                require(hmac.compare_digest(signature, hmac.new(key, body, hashlib.sha256).digest()), code="invalid_cursor")
                value = json.loads(body)
                require(value["user"] == user and value["filters"] == filters, code="invalid_cursor")
                return value["position"]
            except (ValueError, KeyError, TypeError):
                raise AuditError(400, "invalid_cursor") from None
        body = canonical({"user": user, "filters": filters, "position": position}).encode()
        return base64.urlsafe_b64encode(hmac.new(key, body, hashlib.sha256).digest() + body).decode()

    def list_requests(self, user, params, trace=None):
        allowed = {"from", "to", "model", "status", "cursor", "limit"}
        require(not set(params) - allowed, code="invalid_query")
        try:
            limit = int(params.get("limit", 20))
            require(1 <= limit <= 100, code="invalid_limit")
            start, end = float(params.get("from", 0)), float(params.get("to", self.clock()))
            require(math.isfinite(start) and math.isfinite(end) and start <= end, code="invalid_query")
        except (TypeError, ValueError):
            raise AuditError(400, "invalid_query") from None
        filters = {k: v for k, v in params.items() if k != "cursor"}
        filters["trace"] = trace
        with self.transaction():
            if trace:
                require(self.db.execute("SELECT id FROM traces WHERE id=? AND user_id=? AND expires>?", (trace, user, self.clock())).fetchone(), 404, "not_found")
            position = self.cursor(user, filters, token=params["cursor"]) if params.get("cursor") else None
            sql = "SELECT * FROM requests WHERE user_id=? AND expires>? AND created>=? AND created<=?"
            args = [user, self.clock(), start, end]
            for field, val in (("trace_id", trace), ("model", params.get("model")), ("status", params.get("status"))):
                if val:
                    sql += " AND " + field + "=?"; args.append(val)
            if position:
                sql += " AND (created>? OR (created=? AND id>?))"; args.extend((position[0], position[0], position[1]))
            rows = self.db.execute(sql + " ORDER BY created,id LIMIT ?", args + [limit + 1]).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            items = [{k: r[k] for k in ("id", "trace_id", "model", "status", "capture_state", "created")} for r in rows]
            nxt = self.cursor(user, filters, [rows[-1]["created"], rows[-1]["id"]]) if more else None
            self.record_read(user, trace or "requests", 200)
            return {"data": items, "next_cursor": nxt}

    def record_read(self, user, target, status):
        self.db.execute("INSERT INTO reads VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, user, target, self.clock(), status,
                                                                 self.clock() + self.policy["metadata_retention_days"] * 86400))

    def detail(self, user, request_id, params):
        require(not set(params) - {"include_content", "cursor", "limit"}, code="invalid_query")
        require(params.get("include_content", "false") in ("true", "false"), code="invalid_query")
        try:
            limit = int(params.get("limit", 100))
        except (TypeError, ValueError):
            raise AuditError(400, "invalid_limit") from None
        require(1 <= limit <= 100, code="invalid_limit")
        include = params.get("include_content") == "true"
        filters = {"request": request_id, "include_content": include, "limit": limit}
        with self.transaction():
            row = self.owned_request(user, request_id)
            pos = self.cursor(user, filters, token=params["cursor"]) if params.get("cursor") else 0
            records = self.db.execute("SELECT * FROM events WHERE user_id=? AND request_id=? AND seq>? AND expires>? ORDER BY seq LIMIT ?",
                                      (user, request_id, pos, self.clock(), limit + 1)).fetchall()
            more = len(records) > limit
            events = []
            page_bytes = 0
            for event in records[:limit]:
                item = {k: event[k] for k in ("seq", "event_id", "attempt_id", "source", "type", "received", "occurred", "payload_state")}
                item["meta"] = json.loads(event["meta"])
                if event["payload_id"]:
                    payload = self.db.execute("SELECT * FROM payloads WHERE id=?", (event["payload_id"],)).fetchone()
                    if not payload or payload["expires"] <= self.clock() or payload["data"] is None:
                        item["payload_state"] = "expired"
                    elif include:
                        require(hashlib.sha256(payload["data"].encode()).hexdigest() == payload["digest"], 503, "payload_integrity_error")
                        item["payload"] = json.loads(payload["data"])
                item_bytes = len(canonical(item).encode())
                if events and page_bytes + item_bytes > 8 * 1024 * 1024:
                    more = True
                    break
                events.append(item)
                page_bytes += item_bytes
            tools = [dict(r) for r in self.db.execute("SELECT * FROM tool_calls WHERE request_id=? ORDER BY attempt_id,tool_call_id LIMIT 100", (request_id,))]
            for tool in tools:
                executions = self.db.execute("SELECT execution_id,start_event,terminal_event,terminal_type FROM executions WHERE user_id=? AND request_id=? AND attempt_id=? AND tool_call_id=? LIMIT 100",
                                              (user, request_id, tool["attempt_id"], tool["tool_call_id"])).fetchall()
                tool["execution_state"] = "agent_reported" if executions else "not_reported"
                tool["executions"] = [dict(e, missing_start=e["start_event"] is None, missing_end=e["terminal_event"] is None) for e in executions]
                count = self.db.execute('SELECT COUNT(*) FROM executions WHERE user_id=? AND request_id=? AND attempt_id=? AND tool_call_id=?',
                                        (user, request_id, tool['attempt_id'], tool['tool_call_id'])).fetchone()[0]
                tool['executions_truncated'] = count > len(executions)
            tool_count = self.db.execute('SELECT COUNT(*) FROM tool_calls WHERE request_id=?', (request_id,)).fetchone()[0]
            self.record_read(user, request_id, 200)
            request = {k: row[k] for k in ("id", "user_id", "agent_id", "trace_id", "model", "status", "capture_state", "created")}
            request.update(json.loads(row['meta']))
            return {"request": request, "events": events, "tool_calls": tools,
                    "tool_calls_truncated": tool_count > len(tools),
                    "summary_note": "Tool summaries are bounded to 100 items; paginate events for the complete history.",
                    "next_cursor": self.cursor(user, filters, events[-1]['seq']) if more and events else None}

    def maintain(self):
        now = self.clock()
        with self.transaction():
            for row in self.db.execute("SELECT * FROM requests WHERE capture_state='pending' AND deadline<? AND expires>? LIMIT 100", (now, now)).fetchall():
                self.append({"user_id": row["user_id"], "agent_id": row["agent_id"]}, row, row["id"] + ":incomplete", "capture.incomplete", {"reason": "final_event_missing"})
                self.db.execute("UPDATE requests SET capture_state='incomplete' WHERE id=?", (row["id"],))
            self.db.execute("UPDATE payloads SET data=NULL WHERE expires<=?", (now,))
            self.db.execute("DELETE FROM executions WHERE request_id IN (SELECT id FROM requests WHERE expires<=?)", (now,))
            self.db.execute("DELETE FROM tool_calls WHERE request_id IN (SELECT id FROM requests WHERE expires<=?)", (now,))
            self.db.execute("DELETE FROM attempts WHERE request_id IN (SELECT id FROM requests WHERE expires<=?)", (now,))
            self.db.execute("DELETE FROM events WHERE expires<=?", (now,))
            self.db.execute("DELETE FROM payloads WHERE id NOT IN (SELECT payload_id FROM events WHERE payload_id IS NOT NULL)")
            self.db.execute("DELETE FROM requests WHERE expires<=?", (now,))
            self.db.execute("DELETE FROM traces WHERE expires<=?", (now,))
            self.db.execute("DELETE FROM reads WHERE expires<=?", (now,))

    def close(self):
        self.db.close()
