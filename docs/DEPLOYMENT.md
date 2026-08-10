# SenseNova Present 部署指南

## 1. 推荐拓扑

单个 WebUI 服务负责 SQLite 和任务调度，模型、生图与搜索服务通过 HTTP 调用。静态资源和生成工作区写入一个持久化的 `STUDIO_DATA_DIR`。

不建议两个 WebUI 进程同时写同一份 SQLite 数据库。需要多个访问入口时，应让它们反向代理到同一个服务端，而不是共享数据库文件各自启动。

## 2. 配置

```bash
cp .env.example .env
chmod 600 .env
```

至少确认主模型 URL、模型名和密钥。部署前运行：

```bash
./start.sh --check --no-install --no-browser-install
```

输出只显示脱敏后的密钥状态，不打印完整 Secret。

## 3. Linux / CCI 服务

首次在维护窗口执行一次普通启动以安装依赖：

```bash
./start.sh --host 0.0.0.0 --port 8001
```

共享 AFS 的 CCI 环境可使用不会改写虚拟环境的脚本：

```bash
SENSENOVA_DEPLOYMENT_NAME=hjt-cpu-32c \
SENSE_NOVA_LOCAL_PORT=8001 \
STUDIO_LANGUAGE=zh \
./scripts/run_cci_service.sh
```

该脚本会读取项目根目录 `.env`，也可用 `SENSENOVA_ENV_FILE=/secure/path/service.env` 指定配置。重启时只终止并重新启动上述 WebUI 进程，不需要重启 CCI。

## 4. systemd 示例

```ini
[Unit]
Description=SenseNova Present WebUI
After=network-online.target

[Service]
Type=simple
User=sensenova
WorkingDirectory=/opt/sensenova-present
EnvironmentFile=/etc/sensenova-present.env
ExecStart=/opt/sensenova-present/start.sh --host 0.0.0.0 --port 8001 --no-install --no-browser-install
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## 5. Docker

```bash
cp .env.example .env
docker compose up --build -d
docker compose logs -f sensenova-present
```

Compose 使用命名卷保存 `/data`。升级镜像前先备份该卷；删除容器不会删除卷，但 `docker compose down -v` 会删除数据，请勿在生产环境执行。

## 6. macOS / Windows

macOS 使用 `./start.sh`，Windows 使用 `start.ps1`。首次运行需联网下载 Python 依赖和 Chromium。Windows 服务化可使用任务计划程序或 NSSM，让命令固定为：

```powershell
powershell.exe -ExecutionPolicy Bypass -File C:\SenseNovaPresent\web_demo\start.ps1 -Language zh -HostAddress 0.0.0.0 -Port 8001 -NoInstall -NoBrowserInstall
```

## 7. 反向代理

反向代理需保留长轮询/流式请求所需的较长超时，并将请求完整转发到 `http://127.0.0.1:8001`。若通过 HTTPS 发布，建议在代理层设置安全 Cookie、上传大小和访问控制。

## 8. 发布检查

1. `./start.sh --check --no-install --no-browser-install` 通过。
2. `/healthz` 返回 `{"ok": true}`。
3. 中文与英文启动参数均能打开首页。
4. 模型最小请求、生图和搜索按部署需求连通。
5. 创建一个 2–3 页静态 Smoke Case，确认 HTML、预览图与导出包完整。
6. 备份 `STUDIO_DATA_DIR`，确认恢复流程。
7. 检查 `.env`、数据库和日志未被打包进发布物。
