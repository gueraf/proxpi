#!/usr/bin/env bash
# Build and start the parallel-mode proxpi container.
set -euo pipefail

cd "$(dirname "$0")"

docker compose up -d --build

echo
echo "proxpi is running at http://127.0.0.1:5000"
echo "  Health: curl http://127.0.0.1:5000/health"
echo "  Use:    UV_DEFAULT_INDEX=http://127.0.0.1:5000/index/ uv sync"
echo "  Logs:   docker compose logs -f proxpi"
echo "  Stop:   docker compose down"
