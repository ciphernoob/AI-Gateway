#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
(cd admin/web && npm ci --no-audit --no-fund && npm run build)
mkdir -p .build
(cd admin && CGO_ENABLED=0 GOOS=linux go build -trimpath -o ../.build/gateway-admin .)
