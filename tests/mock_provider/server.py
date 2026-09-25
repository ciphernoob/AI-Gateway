"""Deterministic OpenAI-compatible test service. Never expose its control API."""
import argparse
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "normal"
        self.delay = 0.03
        self.requests = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_json(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/healthz":
            return self.send_json(200, {"ok": True})
        if self.path == "/control":
            with self.server.state.lock:
                return self.send_json(200, {"requests": self.server.state.requests, "mode": self.server.state.mode})
        self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 2 * 1024 * 1024:
            self.close_connection = True
            return self.send_json(413, {"error": "too_large"})
        try:
            body = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeDecodeError):
            return self.send_json(400, {"error": "invalid_json"})
        if self.path == "/control":
            with self.server.state.lock:
                self.server.state.mode = body.get("mode", "normal")
                self.server.state.delay = float(body.get("delay", 0.03))
                if body.get("reset", True):
                    self.server.state.requests.clear()
            return self.send_json(200, {"ok": True})
        expected = "Bearer " + os.environ.get(os.environ.get("MOCK_KEY_ENV", "PROVIDER_KEY_A"), "test-provider-key")
        if self.headers.get("Authorization") != expected:
            return self.send_json(401, {"error": {"message": "bad provider credential", "type": "authentication_error", "code": "invalid_key"}})
        with self.server.state.lock:
            self.server.state.requests.append({"body": body, "host": self.headers.get("Host"), "credential_ok": True,
                                               "gateway_header_present": bool(self.headers.get("X-Gateway-User"))})
            mode, delay = self.server.state.mode, self.server.state.delay
        try:
            if mode == "timeout":
                time.sleep(max(delay, 8))
            if mode in ('quota', 'quota_usage', 'quota_402', 'quota_large', 'rate_limit', 'auth_error', 'custom_quota'):
                code = 'rate_limit_exceeded' if mode == 'rate_limit' else ('invalid_api_key' if mode == 'auth_error' else 'insufficient_quota')
                if mode == 'custom_quota': code = 'credits_empty'
                result = {'error': {'code': code, 'type': code, 'message': 'mock quota refusal'}}
                if mode == 'quota_usage': result['usage'] = {'prompt_tokens': 3, 'completion_tokens': 1, 'total_tokens': 4}
                if mode == 'quota_large': result['error']['message'] = 'x' * 70000
                return self.send_json(401 if mode == 'auth_error' else (402 if mode == 'quota_402' else 429), result)
            if mode in ("429", "500", "502"):
                return self.send_json(int(mode), {"error": {"message": "mock failure", "type": "provider_error", "code": mode},
                                                 "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})
            usage = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
            if mode == "bad_usage":
                usage["total_tokens"] = -1
            message = {"role": "assistant", "content": "Hello 世界 from " + os.environ.get("MOCK_NAME", "mock")}
            has_result = any(m.get("role") == "tool" for m in body.get("messages", []))
            if body.get("tools") and not has_result:
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_001", "type": "function", "function": {"name": "weather", "arguments": '{"city":"北京"}'}},
                    {"id": "call_002", "type": "function", "function": {"name": "clock", "arguments": '{"zone":"UTC"}'}}]}
            finish = "tool_calls" if "tool_calls" in message else "stop"
            result = {"id": "chatcmpl-mock", "object": "chat.completion", "model": body.get("model"),
                      "choices": [{"index": i, "message": message, "finish_reason": finish} for i in range(body.get("n", 1))]}
            if mode != "no_usage":
                result["usage"] = usage
            if not body.get("stream"):
                return self.send_json(200, result)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()

            def event(value):
                data = ("data: " + (json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value) + "\r\n\r\n").encode()
                # Deliberately split JSON lines and multibyte Unicode across writes.
                for offset in range(0, len(data), 7):
                    self.wfile.write(data[offset:offset + 7])
                    self.wfile.flush()
                time.sleep(delay)

            def chunk(delta, reason=None, index=0):
                return {"id": result["id"], "object": "chat.completion.chunk", "model": result["model"],
                        "choices": [{"index": index, "delta": delta, "finish_reason": reason}], "usage": None}

            event(chunk({"role": "assistant"}))
            for choice in result["choices"]:
                i = choice["index"]
                if "tool_calls" in message:
                    for pos in range(3):
                        for idx, tool in enumerate(message["tool_calls"]):
                            args = tool["function"]["arguments"]
                            delta = {"index": idx, "function": {"arguments": args[pos::3]}}
                            # Contiguous slices; interleave the two tool streams.
                            start, end = len(args) * pos // 3, len(args) * (pos + 1) // 3
                            delta["function"]["arguments"] = args[start:end]
                            if pos == 0:
                                delta.update(id=tool["id"], type="function")
                                delta["function"]["name"] = tool["function"]["name"]
                            event(chunk({"tool_calls": [delta]}, index=i))
                else:
                    event(chunk({"content": "Hello 世界 "}, index=i))
                    if mode == "interrupt":
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                    event(chunk({"content": "from " + os.environ.get("MOCK_NAME", "mock")}, index=i))
                event(chunk({}, finish, i))
            if mode != "no_usage" and body.get("stream_options", {}).get("include_usage"):
                event({"choices": [], "usage": usage})
            event("[DONE]")
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True


def make_server(host="0.0.0.0", port=8000):
    server = ThreadingHTTPServer((host, port), Handler)
    server.state = State()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    make_server(port=args.port).serve_forever()
