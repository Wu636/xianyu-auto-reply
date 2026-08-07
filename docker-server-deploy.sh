#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.server.yml}"

compose() {
  docker compose --env-file .env -f "$COMPOSE_FILE" "$@"
}

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "请先安装并启动 Docker Engine 与 Docker Compose。" >&2
  exit 1
fi

if [ ! -f global_config.yml ]; then
  echo "缺少 global_config.yml，请把项目完整上传到服务器。" >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  if command -v openssl >/dev/null 2>&1; then
    secret_key="$(openssl rand -hex 32)"
    jwt_key="$(openssl rand -hex 32)"
    sed -i "s#^SECRET_ENCRYPTION_KEY=.*#SECRET_ENCRYPTION_KEY=${secret_key}#" .env
    sed -i "s#^JWT_SECRET_KEY=.*#JWT_SECRET_KEY=${jwt_key}#" .env
  else
    echo "已生成 .env，请手动填写 SECRET_ENCRYPTION_KEY 和 JWT_SECRET_KEY。" >&2
    exit 1
  fi
  echo "已创建 .env；密钥只生成一次，请将该文件妥善备份。"
fi

case "${1:-up}" in
  up)
    compose up -d --build
    echo "服务已启动；默认监听 127.0.0.1:8090，请通过 Caddy/Nginx 的 HTTPS 域名访问。"
    ;;
  restart)
    compose up -d --build
    ;;
  stop)
    compose stop
    ;;
  status)
    compose ps
    ;;
  logs)
    compose logs -f xianyu-app
    ;;
  *)
    echo "用法：$0 [up|restart|stop|status|logs]" >&2
    exit 2
    ;;
esac
