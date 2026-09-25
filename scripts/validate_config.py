"""Validate operator YAML and atomically produce a private runtime snapshot."""
import argparse
import copy
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml

SAFE_INT = 2**53 - 1
ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SCOPES = {"chat:write", "usage:read", "audit:read", "audit:write"}
PLUGINS = {"identity", "unified_api", "fallback", "usage_collector", "budget", "user_quota", "observability", "audit"}


class ConfigError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ConfigError("YAML mapping keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def require(condition, field):
    if not condition:
        raise ConfigError("Invalid configuration field: " + field)


def integer(value, field, minimum=0, maximum=SAFE_INT):
    require(type(value) is int and minimum <= value <= maximum, field)
    return value


def identifier(value, field):
    require(isinstance(value, str) and ID.fullmatch(value) is not None, field)


def url(value, field):
    require(isinstance(value, str), field)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ConfigError("Invalid configuration field: " + field) from None
    require(parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and not any(c.isspace() or ord(c) < 32 for c in value), field)
    require(port is None or 1 <= port <= 65535, field)
    return parsed


def validate(raw, environment=None):
    env = os.environ if environment is None else environment
    c = copy.deepcopy(raw)
    require(isinstance(c, dict), "root")
    secrets = []

    def secret(name, field):
        require(isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", name), field)
        value = env.get(name)
        require(isinstance(value, str) and 16 <= len(value) <= 4096
                and not any(ord(ch) < 32 for ch in value), field + " (environment value missing or invalid)")
        secrets.append(value)
        return value

    try:
        require(c["version"] == 1 and c["timezone"] == "Asia/Shanghai", "version/timezone")
        flags = c["plugins"]
        require(set(flags) == PLUGINS and all(type(v) is bool for v in flags.values()), "plugins")
        require(flags["identity"] and flags["unified_api"], "plugins.identity/unified_api")
        fallback = c.setdefault('fallback', {'on_key_quota_exhausted': False})
        require(isinstance(fallback, dict) and not set(fallback) - {'on_key_quota_exhausted'}, 'fallback')
        fallback.setdefault('on_key_quota_exhausted', False)
        require(type(fallback['on_key_quota_exhausted']) is bool, 'fallback.on_key_quota_exhausted')
        require(not (flags["budget"] or flags["user_quota"] or flags["audit"]) or flags["usage_collector"], "plugins.usage_collector")
        r = c["request"]
        for name in ("max_bytes", "connect_timeout_ms", "read_timeout_ms", "total_timeout_ms", "default_output_tokens"):
            integer(r[name], "request." + name, 1)
        integer(r["max_bytes"], "request.max_bytes", 1, 16 * 1024 * 1024)
        integer(r["max_attempts"], "request.max_attempts", 1, 2)
        require(r["total_timeout_ms"] >= max(r["connect_timeout_ms"], r["read_timeout_ms"]), "request.total_timeout_ms")
        redis = c["redis"]
        require(isinstance(redis["host"], str) and re.fullmatch(r"[A-Za-z0-9_.-]+", redis["host"]), "redis.host")
        integer(redis["port"], "redis.port", 1, 65535)
        for name in ("timeout_ms", "max_pending", "recovery_interval"):
            integer(redis[name], "redis." + name, 1)
        redis["password"] = secret(redis["password_env"], "redis.password_env")
        require(isinstance(c["users"], dict) and c["users"], "users")
        for uid, user in c["users"].items():
            identifier(uid, "users.id")
            require(type(user.get("disabled", False)) is bool, "users.disabled")
            for name in ("daily_token_limit", "monthly_token_limit"):
                if user.get(name) is not None:
                    integer(user[name], "users." + name)
        seen = set()
        require(isinstance(c["api_keys"], list) and c["api_keys"], "api_keys")
        for key in c["api_keys"]:
            require(key["user_id"] in c["users"], "api_keys.user_id")
            require(type(key.get("disabled", False)) is bool, "api_keys.disabled")
            key["disabled"] = key.get("disabled", False) or c["users"][key["user_id"]].get("disabled", False)
            if key.get("agent_id"):
                identifier(key["agent_id"], "api_keys.agent_id")
            else:
                key.pop("agent_id", None)
            require(isinstance(key["scopes"], list) and set(key["scopes"]) <= SCOPES, "api_keys.scopes")
            require("audit:write" not in key["scopes"] or bool(key.get("agent_id")), "api_keys.agent_id")
            digest = hashlib.sha256(secret(key["key_env"], "api_keys.key_env").encode()).hexdigest()
            require(digest not in seen, "api_keys.duplicate_identity")
            seen.add(digest)
            key["digest"] = digest
        for pid, provider in c["providers"].items():
            identifier(pid, "providers.id")
            require(type(provider.get("disabled", False)) is bool, "providers.disabled")
            parsed = url(provider["base_url"], "providers.base_url")
            provider["host"] = parsed.hostname
            provider["scheme"] = parsed.scheme
            provider["port"] = parsed.port or (443 if parsed.scheme == "https" else 80)
            provider["path"] = parsed.path.rstrip("/") + "/v1/chat/completions"
            provider["authority"] = parsed.netloc
            provider["key"] = secret(provider["key_env"], "providers.key_env")
            codes = provider.setdefault('quota_exhaustion_codes', ['insufficient_quota'])
            require(isinstance(codes, list) and 1 <= len(codes) <= 16 and all(
                isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code) for code in codes), 'providers.quota_exhaustion_codes')
        for name, model in c["models"].items():
            identifier(name, "models.id")
            require(1 <= len(model["candidates"]) <= 2, "models.candidates")
            for candidate in model["candidates"]:
                require(candidate["provider"] in c["providers"], "models.provider")
                require(isinstance(candidate["model"], str) and 0 < len(candidate["model"]) <= 200, "models.model")
                for k in ("context_tokens", "max_output_tokens", "n_max"):
                    integer(candidate[k], "models." + k, 1)
                for k in ("input_rate", "output_rate"):
                    integer(candidate[k], "models." + k)
                identifier(candidate["price_version"], "models.price_version")
                require(type(candidate["supports_stream_usage"]) is bool, "models.supports_stream_usage")
                require(isinstance(candidate["capabilities"], list) and set(candidate["capabilities"]) <= {"stream", "tools"}, "models.capabilities")
                require(r["default_output_tokens"] <= candidate["max_output_tokens"], "request.default_output_tokens")
                integer((candidate["context_tokens"] * candidate["input_rate"] + candidate["max_output_tokens"] * candidate["output_rate"]) * candidate["n_max"], "models.price_product")
            model["candidates"] = [x for x in model["candidates"] if not c["providers"][x["provider"]].get("disabled", False)]
            require(bool(model["candidates"]), "models.enabled_candidates")
        require(bool(c["models"]), "models")
        b = c["budget"]
        require(b["currency"] == "USD", "budget.currency")
        identifier(b["epoch"], "budget.epoch")
        integer(b["global_limit"], "budget.global_limit")
        for name, limit in b["model_limits"].items():
            require(name in c["models"], "budget.model_limits")
            integer(limit, "budget.model_limits.limit")
        require(set(b["model_limits"]) == set(c["models"]), "budget.model_limits")
        agents = {k.get("agent_id") for k in c["api_keys"] if k.get("agent_id")}
        require(set(b["agent_limits"]) == agents, "budget.agent_limits")
        for limit in b["agent_limits"].values():
            integer(limit, "budget.agent_limits.limit")
        u = c["usage"]
        for name, value in u.items():
            integer(value, "usage." + name, 1)
        require(u["dedupe_retention_days"] >= u["monthly_retention_days"] >= u["daily_retention_days"] >= u["replay_max_days"], "usage.retention")
        a = c["audit"]
        require(a["mode"] in {"full", "metadata_only", "off"}, "audit.mode")
        require(flags["audit"] == (a["mode"] != "off"), "plugins.audit/audit.mode")
        require(type(a["sample_rate"]) in (int, float) and math.isfinite(a["sample_rate"]) and 0 <= a["sample_rate"] <= 1, "audit.sample_rate")
        identifier(a["policy_version"], "audit.policy_version")
        url(a["base_url"], "audit.base_url")
        for name in ("request_max_bytes", "response_max_bytes_per_attempt", "tool_event_max_bytes", "queue_max_bytes", "queue_max_records", "retry_count", "content_retention_days", "metadata_retention_days"):
            integer(a[name], "audit." + name, 1)
        for name in ("request_max_bytes", "response_max_bytes_per_attempt"):
            integer(a[name], "audit." + name, 1, 4 * 1024 * 1024)
        integer(a['tool_event_max_bytes'], 'audit.tool_event_max_bytes', 1, 256 * 1024)
        require(r['max_bytes'] <= 4 * 1024 * 1024, 'request.max_bytes')
        require(a["metadata_retention_days"] >= a["content_retention_days"], "audit.retention")
        require(a["start_failure_policy"] == "reject", "audit.start_failure_policy")
        require(isinstance(a["redact_fields"], list) and all(isinstance(x, str) and x for x in a["redact_fields"]), "audit.redact_fields")
        a["token"] = secret(a["token_env"], "audit.token_env")
        a["cursor_secret"] = secret(a["cursor_secret_env"], "audit.cursor_secret_env")
    except (KeyError, TypeError, AttributeError, OverflowError):
        raise ConfigError("Missing or incorrectly typed configuration field") from None
    c["known_secrets"] = sorted(set(secrets), key=len, reverse=True)
    return c


def load(path, environment=None):
    try:
        raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=UniqueLoader)
    except yaml.YAMLError:
        raise ConfigError("Invalid YAML syntax") from None
    return validate(raw, environment)


def write_snapshot(config, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".config-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(config, output, ensure_ascii=False, allow_nan=False)
        os.chmod(name, 0o600)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        config = load(args.source)
        if args.output:
            write_snapshot(config, args.output)
    except (ConfigError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    print("Configuration valid" + ("; private snapshot written" if args.output else ""))


if __name__ == "__main__":
    main()
