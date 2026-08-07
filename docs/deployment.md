# 部署与运行指南

> 返回：[README](../README.md) ｜ 相关：[配置说明](configuration.md) ｜ [常见问题](faq.md)

本页保存 README 中不适合展开的部署细节，README 只保留最短启动路径。

## 环境要求

- **Python**: 3.11+
- **Node.js**: 16+（用于 PyExecJS 执行 JavaScript）
- **系统**: Windows / Linux / macOS
- **架构**: x86_64 (amd64) / ARM64 (aarch64)
- **Docker**: 20.10+（Docker 部署）
- **Docker Compose**: 2.0+（Docker 部署）
- **浏览器依赖**: Playwright Chromium（本地运行需要安装）
- **资源建议**: 建议 2GB+ 内存，预留 10GB+ 存储空间

## 方式一：使用部署脚本（推荐）

### Linux / macOS

```bash
git clone https://github.com/GuDong2003/xianyu-auto-reply-fix.git
cd xianyu-auto-reply-fix
chmod +x docker-deploy.sh
./docker-deploy.sh
```

脚本会自动检查依赖、创建目录、构建镜像并启动服务。

默认访问地址：

- `docker-compose.yml`：`http://localhost:9000`
- `docker-compose-cn.yml`：`http://localhost:8000`

### Windows

```cmd
git clone https://github.com/GuDong2003/xianyu-auto-reply-fix.git
cd xianyu-auto-reply-fix
docker-deploy.bat
```

默认访问地址：

- `docker-compose.yml`：`http://localhost:9000`
- `docker-compose-cn.yml`：`http://localhost:8000`

## 方式二：手动使用 Docker Compose

### 默认配置

```bash
git clone https://github.com/GuDong2003/xianyu-auto-reply-fix.git
cd xianyu-auto-reply-fix
docker compose up -d
```

访问：`http://localhost:9000`

### 国内构建配置

```bash
git clone https://github.com/GuDong2003/xianyu-auto-reply-fix.git
cd xianyu-auto-reply-fix
docker compose -f docker-compose-cn.yml up -d --build
```

访问：`http://localhost:8000`

## 方式三：本地运行

```bash
git clone https://github.com/GuDong2003/xianyu-auto-reply-fix.git
cd xianyu-auto-reply-fix

python -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
# Linux 可能还需要：playwright install-deps chromium

python Start.py
```

访问：`http://localhost:8090`

> 本地运行请确保已安装 Node.js，否则 `PyExecJS` 相关功能无法正常使用。

## 方式四：常驻服务器运行（自动回复 / 自动发货）

自动发货依赖常驻的 Python 进程、WebSocket 连接、定时任务和持久化数据库。Vercel 的函数模型适合短请求和前端页面，这个项目建议使用带持久磁盘的 Docker 主机；Render、Railway、Fly.io 或一台 VPS 都可以。

### 一台 VPS 使用 Docker Compose

服务器安装 Docker 后，在项目目录执行：

```bash
chmod +x docker-server-deploy.sh
./docker-server-deploy.sh up
./docker-server-deploy.sh logs
```

脚本首次运行会复制 `.env.example` 并生成两组稳定密钥；也可以手动编辑 `.env` 后执行同一命令。后续使用 `./docker-server-deploy.sh restart`、`status`、`stop` 管理服务。

管理界面容器默认绑定本机 `127.0.0.1:8090`，通过下面的 HTTPS 反向代理域名访问。`xianyu_data`、`xianyu_logs` 和 `xianyu_uploads` 三个 Docker volume 会在重启后保留数据库、Cookie 加密文件、日志、上传图片和内容商品 ZIP。首次登录后，在 Web 界面重新填入闲鱼 Cookie、夸克 Cookie 及 AI Key；稳定的加密密钥必须长期保留。

### 外网访问与 HTTPS

生产环境建议让 Docker 只监听服务器本机的 8090 端口，再用 Caddy 或 Nginx 对外提供 HTTPS。仓库中的 `Caddyfile.example` 可直接作为 Caddy 配置：将域名替换为自己的域名，并把 DNS 指向服务器；公网只开放 80/443，管理 Cookie 和 API Key 的流量全部通过 HTTPS 传输。

### Render Blueprint

仓库根目录的 `render.yaml` 已包含 Docker 构建、`/health` 健康检查和 `/app/data` 持久磁盘。Render 控制台导入仓库后选择 Blueprint 部署即可。该服务请选择持续运行的实例规格，免费休眠实例会暂停自动发货任务。Render 自动注入的 `PORT` 已由 `Start.py` 读取。

### 内容商品化导出

“链接商品化”页面会把公开文章链接或粘贴的正文整理为商品素材，并在 `/app/data/content_products` 保存 Markdown、原文快照和 ZIP。生成结果会载入“商品发布”表单，修改标题、详情、价格、类目和图片后再发布；“生成并上架”适用于已确认来源状态的内容。

如果希望付款后自动发送交付内容，在页面填写“自动发货文字 / 永久分享链接”。系统会为该商品创建文本卡券和按标题匹配的发货规则；链接应使用买家可访问的长期有效分享地址。留空时只生成内容包，不会创建发货规则。

## 多架构支持

支持的架构：

- `linux/amd64` - Intel / AMD 处理器
- `linux/arm64` - ARM64 处理器

构建方式：

- 提供 `build-multi-arch.sh` 多架构构建脚本
- 支持使用 Docker Buildx 构建 amd64 / arm64 镜像
- Docker 部署和本地运行可在对应架构环境中使用

说明：

- 当前仓库未包含 GitHub Actions 自动构建配置
- 镜像仓库地址请以实际发布情况为准

## 访问地址

部署完成后，您可以通过以下方式访问系统：

| 场景 | Web 管理界面 | API 文档 | 健康检查 |
| --- | --- | --- | --- |
| Docker Compose 默认配置 | `http://localhost:9000` | `http://localhost:9000/docs` | `http://localhost:9000/health` |
| Docker Compose 国内配置 | `http://localhost:8000` | `http://localhost:8000/docs` | `http://localhost:8000/health` |
| 本地运行 | `http://localhost:8090` | `http://localhost:8090/docs` | `http://localhost:8090/health` |

默认管理员账号（首次初始化且未自定义密码时）：

- 用户名：`admin`
- 密码：`admin123`

> ⚠️ 首次登录后请立即修改默认密码。
