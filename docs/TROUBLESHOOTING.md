# 故障处理

## 首次启动很慢

第一次会下载 Python 依赖和 Chromium，属于正常现象。后续启动可以使用：

```bash
./start.sh --no-install --no-browser-install
```

仅在虚拟环境与浏览器已经准备好时使用这两个参数。

## 找不到 Python 或 uv

- Python 版本必须满足 Studio 的 `>=3.12`。
- `start.sh` / `start.ps1` 会尝试通过 `pip --user` 安装 uv。
- 企业环境不能访问 PyPI 时，请配置内部 Python 镜像后再启动。

## Chromium 缺失或启动失败

重新安装浏览器：

```bash
PLAYWRIGHT_BROWSERS_PATH=./studio/data/ms-playwright \
  ./inference/.venv/bin/python -m playwright install chromium
```

Debian/Ubuntu 缺少系统库时，Docker 构建会使用 `playwright install --with-deps` 自动安装；裸机可执行：

```bash
./inference/.venv/bin/python -m playwright install --with-deps chromium
```

## Windows 上生成失败

sn-ppt-web（内部使用 `long-horizon-presenter` Harness，并路由到 `sn-ppt-web-zh/en`）依赖 POSIX shell 和 Unix 文件锁。请在 Docker Desktop 或 WSL2 Ubuntu 中
运行，不要用裸 Windows Python 执行生成任务。

## Skill 显示不可用

确认以下文件存在：

```text
bundled/static-ppt-skill-suite/skills/long-horizon-presenter/SKILL.md
bundled/static-ppt-skill-suite/harnesses/long-horizon-presenter/distill_ppt.py
```

运行：

```bash
./start.sh --check
```

检查输出中的 `presenter_suite` 与 `default_skill`。

## 模型不可达

先从部署机器直接请求模型服务：

```bash
curl "$SENSENOVA_MODEL_BASE_URL/models" \
  -H "Authorization: Bearer $SENSENOVA_MODEL_API_KEY"
```

不同服务对 `/models` 的支持可能不同；至少要确认 DNS、路由、端口和证书可用。WebUI 的
`/healthz` 只表示 Web 服务健康，不代表外部模型健康。

## 生图或搜索失败

- 检查 URL 是否包含服务要求的 `/v1`。
- 检查 Key 是否在启动 WebUI 的同一个进程环境中。
- `.env` 修改后需要重启 **WebUI 服务进程**，不需要重启整台机器或容器实例。
- 这两项不是启动必填项；未配置时首页会提示可在「用户 → 设置」补充，任务仍可按可用能力继续。

## macOS 一直显示“排队中”

V1 调度器不再假设系统存在 Linux `/proc`：Linux 优先读取 `/proc`，macOS 使用 `ps`，Windows
使用 PowerShell/CIM。升级旧包后只需重启 WebUI 服务进程，尚未启动的 queued 任务会由启动恢复逻辑重新调度。

## 附件解析或中文字体缺失

普通执行 `./start.sh` 会在首次运行时安装完整附件解析环境和 OFL 字体。检查：

```bash
./start.sh --check --no-install --no-browser-install
```

输出中的 `normalize_python` 与 `font_source_dirs` 应指向 `runtime/` 下的有效路径。不要在首次安装前使用
`--no-install`；该参数会明确跳过环境准备。

## 端口被占用

换一个端口：

```bash
./start.sh --port 8011
```

## 历史记录消失

确认启动时使用了同一个 `STUDIO_DATA_DIR`。升级时不要复制单个 SQLite 主文件而遗漏 WAL；应先
停止旧服务，再完整备份数据目录。

## 页面预览字体不同

- 确认部署机器上存在所选字体，或在用户设置中上传获准使用的字体。
- 重新生成交付物，使字体打包步骤重新执行。
- 浏览器预览缓存可通过强制刷新清除。

## 收集诊断信息

```bash
./start.sh --check
curl http://127.0.0.1:8001/healthz
```

任务日志位于 `STUDIO_DATA_DIR/jobs/`，具体 Deck 工作区位于
`STUDIO_DATA_DIR/workspaces/<workspace>/decks/<deck>/`。分享日志前请移除用户附件、提示词和密钥。
