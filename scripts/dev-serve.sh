#!/usr/bin/env bash
# HypoArgus 可视化服务一键部署/运行脚本（本地开发体验用）。
#
# 本脚本是本会话部署经验的沉淀，供新会话快速把服务跑起来供浏览器体验。
#
# ─────────────────────────────────────────────────────────────────────────────
# 网络配置零改动承诺（硬约束）
# ─────────────────────────────────────────────────────────────────────────────
# 本脚本 **不改动本机任何网络配置**：
#   - 不运行 `tailscale up` / `tailscale serve` / `tailscale funnel`；
#   - 不修改 tailscale 状态、ACL、advertise routes、exit node、hostname；
#   - 不动 iptables / nftables / ufw / firewalld / 路由表 / systemd-networkd；
#   - 不需要 root，不写 /etc。
# 对 tailscale 仅做 **只读** 查询（`tailscale ip -4` / `tailscale status --self --json`），
# 用于在终端打印 tailnet 访问地址——前提是 tailscale 本会话之前已经 up。
#
# “Tailscale 可达”靠的是：把 **vite dev server** 绑到 `0.0.0.0`（一个 vite 启动参数，
# 进程级监听地址，进程退出即失效，不是持久网络配置）。后端始终只绑 `127.0.0.1`，
# 浏览器只访问 vite 的 5173，`/api` 与 `/ws` 由 vite 在本机代理到后端。
#
# ─────────────────────────────────────────────────────────────────────────────
# 用法
# ─────────────────────────────────────────────────────────────────────────────
#   scripts/dev-serve.sh            # 默认：fake 后端（零 token、确定性），vite 绑 0.0.0.0
#   scripts/dev-serve.sh --real     # 真实 DashScope 后端（消耗 token），vite 代理到 8000
#   scripts/dev-serve.sh --local    # vite 只绑 127.0.0.1（不暴露到 tailnet，仅本机访问）
#   scripts/dev-serve.sh --port 5173 --real --local
#   scripts/dev-serve.sh --help
#
# 两条后端路径都依赖 Postgres（.env 的 HYPOARGUS_PG_DSN，建表靠 setup() 幂等）。
# 真实后端额外需要 DASHSCOPE_API_KEY；Langfuse 三变量缺失则自动降级（不阻塞）。

set -euo pipefail

# ── 路径与默认值 ──────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$REPO_ROOT/web"
VITE_PORT=5173
VITE_HOST="0.0.0.0"      # 默认暴露到 tailnet；--local 改为 127.0.0.1
USE_REAL=0
CONDA_ENV="HypoArgus"

usage() {
  sed -n '/^# 用法/,/^# 两条后端/p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

# ── 参数解析 ──────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --real)  USE_REAL=1; shift ;;
    --local) VITE_HOST="127.0.0.1"; shift ;;
    --port)  VITE_PORT="$2"; shift 2 ;;
    --help|-h) usage 0 ;;
    *) echo "未知参数: $1" >&2; usage 1 ;;
  esac
done

# ── 前置检查 ──────────────────────────────────────────────────────────────────
command -v conda >/dev/null 2>&1 || { echo "✗ 未找到 conda（需要 conda 环境 $CONDA_ENV）" >&2; exit 1; }
command -v tailscale >/dev/null 2>&1 || { echo "✗ 未找到 tailscale（tailnet 地址探测需要它，只读使用）" >&2; exit 1; }
[[ -f "$REPO_ROOT/.env" ]] || { echo "✗ 缺少 $REPO_ROOT/.env（需 HYPOARGUS_PG_DSN 等）" >&2; exit 1; }
[[ -f "$REPO_ROOT/e2e/dev_server.py" ]] || { echo "✗ 缺少 e2e/dev_server.py" >&2; exit 1; }

# 端口占用预检
port_busy() { ss -tln 2>/dev/null | awk '{print $4}' | grep -qE ":$1$"; }
for p in "$VITE_PORT"; do
  port_busy "$p" && { echo "✗ 端口 $p 已被占用，请先释放或换 --port" >&2; exit 1; }
done

# ── 加载 .env（DASHSCOPE_API_KEY / HYPOARGUS_PG_DSN / LANGFUSE_*）───────────────
# dev_server.py 会自己 load_dotenv()，但真实 server.py 不显式加载——统一在 shell 层 source 兜底，
# 保证两条路径的子进程都能继承到环境变量。
set -a
# shellcheck disable=SC1091
source "$REPO_ROOT/.env"
set +a

# ── PG 可达性预检（快速失败，避免健康轮询悬空）────────────────────────────────
PG_PREFLIGHT="$(
  cd "$REPO_ROOT"
  conda run --no-capture-output -n "$CONDA_ENV" python - <<'PY' 2>/dev/null || true
import os, socket
dsn = os.environ.get("HYPOARGUS_PG_DSN", "")
host = port = ""
if "@" in dsn and "/" in dsn:
    hp = dsn.split("@", 1)[1].split("/", 1)[0]
    if ":" in hp:
        host, port = hp.rsplit(":", 1)
    else:
        host, port = hp, "5432"
if not host:
    print("NO_DSN"); raise SystemExit
s = socket.socket(); s.settimeout(4)
try:
    s.connect((host, int(port))); print(f"OK {host}:{port}")
except Exception as e:
    print(f"FAIL {host}:{port} {e}")
finally:
    s.close()
PY
)"
case "$PG_PREFLIGHT" in
  OK*)        echo "• Postgres 预检通过：${PG_PREFLIGHT#OK }" ;;
  FAIL*|NO_DSN|"" )
    echo "✗ Postgres 预检失败：${PG_PREFLIGHT:-无 HYPOARGUS_PG_DSN}" >&2
    echo "  两条后端路径都需要可达的 PG（checkpointer）。请检查 .env 的 HYPOARGUS_PG_DSN。" >&2
    exit 1 ;;
esac

if [[ "$USE_REAL" -eq 1 ]]; then
  [[ -n "${DASHSCOPE_API_KEY:-}" ]] || { echo "✗ --real 需要 DASHSCOPE_API_KEY（.env 未配置）" >&2; exit 1; }
  BACKEND_PORT=8000
  BACKEND_LABEL="真实 DashScope 后端（消耗 token）"
else
  BACKEND_PORT=8001
  BACKEND_LABEL="fake 后端（StreamingFakeChat，零 token、确定性）"
  export HYPOARGUS_E2E_HOST="127.0.0.1"
  export HYPOARGUS_E2E_PORT="$BACKEND_PORT"
fi

echo "• 后端：$BACKEND_LABEL  →  127.0.0.1:$BACKEND_PORT（仅本机）"
echo "• 前端：vite  →  $VITE_HOST:$VITE_PORT"

# ── 前端依赖兜底 ──────────────────────────────────────────────────────────────
if [[ ! -x "$WEB_DIR/node_modules/.bin/vite" ]]; then
  echo "• 前端依赖缺失，执行 pnpm install …"
  ( cd "$WEB_DIR" && pnpm install )
fi

# ── 清理陷阱：退出时杀掉两个进程组 ───────────────────────────────────────────
# 关键经验：之前用 `( cd … && conda run … ) &` 时，$! 捕获的是 subshell；
# conda run 被杀后其 python 子进程被 init 收养成孤儿，且 uvicorn 对 SIGTERM
# 不一定即时退出。故开 `set -m`（job control）令每个后台作业进自己的进程组
# （pgid = $!），subshell 内 `exec` 后该 pid 即 conda/vite 本体，其子进程
# （python / esbuild）同组；cleanup 用 kill -TERM -PGID 杀整棵树，再 SIGKILL 兜底。
set -m
BACKEND_PGID=""
VITE_PGID=""
cleanup() {
  trap - EXIT INT TERM  # 防重入
  echo
  echo "• 收到退出信号，停止后端与前端 …"
  for pgid in "$VITE_PGID" "$BACKEND_PGID"; do
    [[ -n "$pgid" ]] && kill -TERM "-$pgid" 2>/dev/null || true
  done
  # 最多 ~3s 优雅退出，未退出则 SIGKILL 兜底
  local i=0
  while (( i++ < 30 )); do
    sleep 0.1
    { [[ -n "$VITE_PGID" ]]    && kill -0 "-$VITE_PGID"    2>/dev/null; } \
    || { [[ -n "$BACKEND_PGID" ]] && kill -0 "-$BACKEND_PGID" 2>/dev/null; } \
    || break
  done
  for pgid in "$VITE_PGID" "$BACKEND_PGID"; do
    [[ -n "$pgid" ]] && kill -KILL "-$pgid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  echo "• 已停止（网络配置零改动，无残留监听）。"
}
trap cleanup EXIT INT TERM

# ── 启动后端（set -m → 独立进程组，pgid=其 pid）──────────────────────────────
# backend 启动需要 cwd=REPO_ROOT（dev_server 的 load_dotenv 从 cwd 向上找 .env）。
export PYTHONPATH=src
if [[ "$USE_REAL" -eq 1 ]]; then
  ( cd "$REPO_ROOT" && exec conda run --no-capture-output -n "$CONDA_ENV" \
      python -m api_layer.server ) &
else
  ( cd "$REPO_ROOT" && exec conda run --no-capture-output -n "$CONDA_ENV" \
      python e2e/dev_server.py ) &
fi
BACKEND_PGID=$!

# ── 等待后端就绪（轮询 /api/agent/graph，两条路径都有）──────────────────────────
echo "• 等待后端就绪 …"
BACKEND_READY=0
for _ in $(seq 1 40); do
  if curl -sf -m 2 "http://127.0.0.1:$BACKEND_PORT/api/agent/graph" >/dev/null 2>&1; then
    BACKEND_READY=1; break
  fi
  sleep 0.5
done
[[ "$BACKEND_READY" -eq 1 ]] || { echo "✗ 后端未在 ~20s 内就绪，检查上方日志" >&2; exit 1; }
echo "  ✓ 后端就绪"

# ── 启动 vite（代理 /api /ws 到后端，注入 X-User-Id: dev-user）───────────────────
export VITE_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
export VITE_WS_BACKEND_URL="ws://127.0.0.1:$BACKEND_PORT"
( cd "$WEB_DIR" && exec "$WEB_DIR/node_modules/.bin/vite" \
    --host "$VITE_HOST" --port "$VITE_PORT" --strictPort ) &
VITE_PGID=$!

# ── 等待 vite 就绪 ────────────────────────────────────────────────────────────
echo "• 等待前端就绪 …"
VITE_READY=0
for _ in $(seq 1 40); do
  if curl -sf -m 2 "http://127.0.0.1:$VITE_PORT/" >/dev/null 2>&1; then
    VITE_READY=1; break
  fi
  sleep 0.5
done
[[ "$VITE_READY" -eq 1 ]] || { echo "✗ 前端未在 ~20s 内就绪" >&2; exit 1; }
echo "  ✓ 前端就绪"

# ── 打印访问地址 ──────────────────────────────────────────────────────────────
TS_IP="$(tailscale ip -4 2>/dev/null || true)"
TS_HOST="$(tailscale status --self --json 2>/dev/null \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || true)"

echo
echo "════════════════════════════════════════════════════════════════"
echo "  HypoArgus 可视化服务已启动（$BACKEND_LABEL）"
echo "──────────────────────────────────────────────────────────────"
echo "  本机访问：    http://127.0.0.1:$VITE_PORT/"
if [[ "$VITE_HOST" == "0.0.0.0" && -n "$TS_IP" ]]; then
  echo "  tailnet 访问：http://$TS_IP:$VITE_PORT/"
  [[ -n "$TS_HOST" ]] && echo "  MagicDNS：    http://$TS_HOST:$VITE_PORT/"
else
  echo "  （--local 模式：仅本机访问，未暴露到 tailnet）"
fi
echo "──────────────────────────────────────────────────────────────"
echo "  Ctrl-C 停止两个进程；网络配置零改动，无残留监听。"
echo "════════════════════════════════════════════════════════════════"
echo
echo "  X-User-Id 由 vite 代理自动注入为 dev-user，无需认证。"
if [[ "$USE_REAL" -eq 0 ]]; then
  echo "  注意：fake 后端的“修订”是平凡的（空 proposals→全 background→终稿≈原文），"
  echo "        用于验证 UI/WS/HITL 通路，不是真实修订质量。"
  echo "        想体验真实 Qwen 修订：$0 --real"
fi
echo

# 保持前台挂起，直到收到信号（cleanup 杀整棵进程树）
wait "$VITE_PGID" 2>/dev/null || true
