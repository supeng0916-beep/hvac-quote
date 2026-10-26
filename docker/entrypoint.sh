#!/bin/sh
set -eu

DATA_DIR="${HVAC_DATA_DIR:-/data}"
RUNS_DIR="${HVAC_RUNS_DIR:-$DATA_DIR/runs}"
PORT="${PORT:-8000}"

mkdir -p "$DATA_DIR" "$RUNS_DIR"

# 允许 docker run <image> hvac-quote ... 或 python ... 做诊断；
# 不传参数时启动报价页面。
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# 容器内以 root 运行，确保 Windows Docker Desktop 的绑定目录可写。
# 对外只暴露报价页面；请通过客户电脑防火墙限制局域网访问。
exec hvac-quote serve \
    --host 0.0.0.0 \
    --port "$PORT" \
    --data "$DATA_DIR" \
    --runs "$RUNS_DIR"
