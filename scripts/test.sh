#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Use a separate Compose project so failure injection never touches the dev stack.
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ai-gateway-test}"
export GATEWAY_PORT="${GATEWAY_PORT:-18080}"
test -f .env || cp .env.example .env
restore() {
  docker compose start redis audit-store >/dev/null || true
  docker compose run --rm config python -m scripts.config_variant normal >/dev/null || true
  docker compose restart gateway >/dev/null || true
}
trap restore EXIT
docker compose up -d --build --wait
docker compose exec -T gateway openresty -p /app/ -c nginx/nginx.conf -t
docker compose run --rm --build test python -m unittest discover -s tests -v
docker compose exec -T gateway resty -I /app/lua /app/tests/lua/run.lua
for variant in fallback both_down single_attempt budget_denied quota_exceeded metrics_off; do
  docker compose run --rm config python -m scripts.config_variant "$variant"
  docker compose restart gateway
  docker compose run --rm test python -m scripts.fault_probe "$variant"
done
for variant in key_quota_off key_quota_on key_quota_same key_quota_single key_quota_budget key_quota_custom; do
  docker compose run --rm config python -m scripts.config_variant "$variant"
  docker compose restart gateway
  docker compose run --rm test python -m tests.quota_switch "$variant"
done
restore
docker compose stop audit-store
docker compose run --rm test python -m scripts.fault_probe audit_down
docker compose start audit-store
docker compose stop redis
docker compose run --rm test python -m scripts.fault_probe redis_down
restore
trap - EXIT
echo 'All automated acceptance checks passed; persistent test volumes retained.'
