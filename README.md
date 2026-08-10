<p align="center">
  <img src="studio/static/sensenova-mark.png" alt="SenseNova Present" width="88">
</p>

<h1 align="center">SenseNova Present WebUI</h1>

<p align="center">
  一套可私有部署、开箱即用的 AI 演示文稿工作台
</p>

<p align="center">
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-WebUI-009688?logo=fastapi&logoColor=white">
  <img alt="Platforms" src="https://img.shields.io/badge/Verified-macOS-6C4CF1">
  <img alt="Languages" src="https://img.shields.io/badge/UI-简体中文%20%7C%20English-18BFA2">
</p>

<p align="center">
  <a href="#3-五分钟启动">快速开始</a> ·
  <a href="docs/CONFIGURATION.md">配置手册</a> ·
  <a href="docs/DEPLOYMENT.md">部署指南</a> ·
  <a href="docs/TROUBLESHOOTING.md">故障排查</a> ·
  <a href="README_EN.md">English</a>
</p>

SenseNova Present 是一套可私有部署的 AI 演示文稿工作台。本发布包固定为：

- **V1 静态演示版**：不展示动态演示入口。
- **免登录单用户模式**：首次打开即可使用，历史记录保存在本地数据目录。
- **sn-ppt-web**：前端统一产品名；`sn-ppt-web` Harness 会按 query 语言自动选择冻结的 `sn-ppt-web-zh` 或 `sn-ppt-web-en` Skill，不依赖开发机上的 AFS 路径。
- **跨平台启动方案**：macOS 已完成实际验证；Linux、WSL2 和 Windows Docker Desktop 提供启动脚本与部署方案，但尚未完成同等强度的全链路回归。

> 模型、生图和搜索服务不包含在 ZIP 中。部署者需要填写自己的兼容 API 地址和密钥。

## 重要：当前已验证范围

这个公开版本仍处于 V1 验证阶段。README 中的“兼容”表示接口和启动方式按相应协议实现，
不等于所有平台、模型与服务都已经完成生产级回归。当前实际验证范围如下：

| 项目 | 当前已验证范围 | 其他实现的状态 |
|---|---|---|
| 操作系统 | **macOS** 已完成安装、启动、生成、预览与导出链路验证 | Linux、WSL2、Windows Docker Desktop 已提供脚本/镜像方案，但尚未完成与 macOS 同等强度的完整回归 |
| 主模型 | 使用 **SenseNova 自有 OpenAI-compatible 多模态模型** 验证；模型需支持 Chat Completions、工具调用和多模态输入 | 其他 OpenAI-compatible 模型可自行配置，但不代表已经验证，能力不足时可能在工具调用、长上下文或视觉输入阶段失败 |
| 生图服务 | 支持 **OpenAI Images-compatible**（`openai_images`，已实测 `gpt-image-2-adobe-2`）和 **SenseNova U1**（`sensenova_u1`）两种 Provider | U1 通过 SenseNova 原生 Images API 接入；其他生图模型尚未逐一验证，且生图能力可以不配置 |
| 搜图服务 | 目前仅验证 **Serper 兼容搜索接口**（`google.serper.dev`） | 其他搜索 API 不能仅靠填写 URL 直接保证兼容；搜图能力可以不配置 |

如果你在上述范围之外部署，请先按照[验证安装](#7-验证安装)运行 2–3 页 Smoke Case，
再用于正式任务。欢迎在 Issue 中反馈操作系统、模型名称、服务协议和复现日志，帮助补充兼容性矩阵。

### 模型与服务接口契约

当前 V1 对上游返回格式有明确要求。仅仅“URL 可访问”并不代表模型能够驱动完整制作流程。

**主模型（必需）**

- OpenAI-compatible `POST {base_url}/chat/completions`，当前以非流式 JSON 方式调用。
- 返回至少包含 `choices[0].message` 与 `choices[0].finish_reason`；建议同时返回标准 `usage.prompt_tokens` 和 `usage.completion_tokens`。
- `message.content` 用于自然语言回复；深度思考可放在 `message.reasoning` 或 `message.reasoning_content`。
- 必须支持 OpenAI 标准 `tools` 输入和 `message.tool_calls` 输出，否则无法稳定执行渲染、检查与子 Agent 协作。
- 必须支持 OpenAI 多模态 `image_url` 内容块，并能读取 `data:image/...;base64,...`，否则 Vision 检查无法工作。
- 当前只对 SenseNova 自有 OpenAI-compatible 多模态模型做过全链路验证；README 不把未实际测试的模型列为“已支持”。

**配图模型（可选）**

- `openai_images`：调用 `POST {image_base_url}/images/generations`，请求体包含 `model`、`prompt`、`size`、`n: 1`。当前实测模型为 `gpt-image-2-adobe-2`。
- `sensenova_u1`：调用 SenseNova 原生 `POST {image_base_url}/images/generations`，使用 U1 支持的 1K 尺寸桶，并发送 `response_format=url`、`output_format=png`。推荐模型名为 `sensenova-u1-fast`。
- 返回必须包含非空 `data` 数组；WebUI/Harness 接受以下任一模式：
  - Base64：`{"data":[{"b64_json":"..."}]}`；
  - URL：`{"data":[{"url":"https://..."}]}`，且该 URL 必须允许服务端直接下载。
- OpenAI Images Provider 不强制发送 `response_format`；SenseNova U1 Provider 会按其原生接口约定请求 URL 返回。

**搜索服务（可选，当前实测 Serper）**

- 请求：`POST {search_base_url}/search`，通过 `X-API-KEY` 鉴权。
- 文本结果读取顶层 `organic[]` 的 `title`、`link`、`snippet`。
- 图片结果读取顶层 `images[]` 的 `title`、`imageUrl`（缺失时回退到 `link`）。
- 如果你的搜索服务字段不同，需要增加适配器，而不是只替换 URL。

## 功能概览

- 从自然语言和附件生成完整的静态 HTML 演示文稿。
- 使用 `sn-ppt-web` Harness 与 `sn-ppt-web-zh/en` 双语冻结 Skill 进行长链路规划、素材准备、逐页制作和视觉检查。
- 支持 PDF、Office、Markdown、文本和图片等常用附件。
- 在 WebUI 中查看制作过程、页面预览、讲稿、检查记录与历史任务。
- 支持预览、播放以及带资源和字体的 HTML 导出。
- 主模型可从 `.env` 注入，也可在 WebUI 中添加和删除；生图与搜索能力可选。
- 内置 45 个白名单 OFL/开源字体，字体准备阶段可离线完成。
- 默认深色主题，同时支持浅色主题和中英文界面。

> 当前公开版本是 V1：仅开放静态演示、免登录、本地单用户模式。请勿直接暴露到不受信任的公网。

## 1. 解压后的目录

```text
SenseNovaPresent-WebUI-.../
├── bundled/static-ppt-skill-suite/
│   ├── skills/sn-ppt-web-zh/
│   ├── skills/sn-ppt-web-en/
│   ├── skills/sn-ppt-web/  # 安装兼容入口
│   └── harnesses/sn-ppt-web/
├── studio/                    FastAPI、前端和本地数据
├── inference/                 WebUI 到静态 Harness 的最小推理适配层
├── docs/
│   ├── CONFIGURATION.md
│   ├── DEPLOYMENT.md
│   └── TROUBLESHOOTING.md
├── scripts/launch.py          跨平台统一启动器
├── start.sh / start.ps1 / start.bat
├── Dockerfile / compose.yaml
└── .env.example
```

ZIP 不包含账号库、历史任务、上传附件、生成结果、密钥、日志、浏览器缓存或虚拟环境。

## 2. 系统要求

### 通用

- 8 GB 内存起步，推荐 16 GB 以上。
- 首次安装需要联网下载 Python 包和 Playwright Chromium。
- 需要能访问你配置的主模型、生图和搜索 API。
- 默认端口为 `8001`。

### macOS

- macOS 13+（Intel / Apple Silicon）。
- Python 3.12，推荐通过 Homebrew 安装：`brew install python@3.12`。
- 生成链路会调用 POSIX shell，macOS 可原生运行。
- **这是当前唯一完成完整实测的桌面系统。**

### Linux

- Ubuntu 22.04+ / Debian 12+ 优先。
- Python 3.12。
- 无桌面环境也可运行；渲染由 Playwright Chromium 完成。
- 当前提供安装和部署方案，尚未完成与 macOS 同等强度的全链路回归。

### Windows

Windows 推荐以下任一方式：

1. **Docker Desktop（最省心）**：Windows 10/11 + WSL2 backend。
2. **WSL2 Ubuntu**：在 WSL 终端中按 Linux 步骤启动。

`start.ps1` 可以原生启动 WebUI，但 `sn-ppt-web` 的生成工具使用 POSIX shell 与
Unix 文件锁。需要完整生成能力时请使用 Docker Desktop 或 WSL2，不建议裸 Windows Python。
当前 Windows 相关方式尚未完成与 macOS 同等强度的全链路回归，请先运行 Smoke Case。

## 3. 五分钟启动

### 3.1 配置

复制模板。发布包默认不预置模型；可以在 `.env` 注册一个部署模型，也可以启动后从
「用户 → 模型配置」添加和删除模型：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
SENSENOVA_MODEL_BASE_URL=http://model-host:8000/v1
SENSENOVA_MODEL_NAME=your-multimodal-model
SENSENOVA_MODEL_API_KEY=EMPTY
SENSENOVA_MODEL_DISPLAY_NAME=My multimodal model
```

这组变量会新增一个独立模型，不会覆盖任何内置模型。若暂不填写，模型列表保持为空；
点击首页的「模型」会直接打开配置界面，配置完成前不能发起生成。

如需图片生成与联网搜索，再填写：

```dotenv
SENSENOVA_IMAGE_PROVIDER=openai_images
SENSENOVA_IMAGE_BASE_URL=https://image.example/v1
SENSENOVA_IMAGE_MODEL=gpt-image-2-adobe-2
SENSENOVA_IMAGE_API_KEY=replace-me

SENSENOVA_SEARCH_BASE_URL=https://google.serper.dev
SENSENOVA_SEARCH_API_KEY=replace-me
```

默认生图 Provider 为 `openai_images`。也可以配置 `SENSENOVA_IMAGE_PROVIDER=sensenova_u1`，
通过 SenseNova 原生 `POST /images/generations` 接入 U1；推荐模型名为 `sensenova-u1-fast`。
上述 OpenAI Images 示例对应目前的实际验证组合：`gpt-image-2-adobe-2` + Serper 兼容搜索接口。
SenseNova U1 适配使用其 1K 尺寸桶并解析 `data[].url`；同时兼容图片 URL、data URL/base64、
`message.images` 和多模态 content block 等常见网关返回；
替换为其他兼容服务时请自行做连通性与生成质量验证。

所有配置项及优先级见 [配置手册](docs/CONFIGURATION.md)。

### 3.2 macOS / Linux / WSL2

```bash
cd /path/to/SenseNovaPresent-WebUI-...
chmod +x start.sh
./start.sh --language zh
```

首次运行会：

1. 检查并安装 `uv`；
2. 创建 `studio/.venv` 和 `inference/.venv`；
3. 按锁文件安装依赖；
4. 安装附件解析环境（MarkItDown、PDF/Office 解析依赖）；
5. 从发布包离线安装 Noto Sans/Serif SC 与交付所需 OFL 字体，并设置 `NORMALIZE_PY`、`PPT_FONT_SOURCE_DIRS`；仅当发布包字体不完整时才尝试联网补齐；
6. 下载 Playwright Chromium；
7. 启动 `http://127.0.0.1:8001`。

上述完整准备只在首次运行或运行环境缺失时执行，产物位于 `runtime/`，不会写入源码目录。
发布 ZIP 已内置 45 个经过白名单校验的 OFL/开源字体，因此字体安装不依赖网络；
其中已补齐 Skill 正向声明的 Smiley Sans 与 IBM Plex Sans；未随包提供的 Inter、JetBrains Mono、
Source Han Sans/Serif 已从字体声明中移除。
为控制授权风险，开发机上的商业字体和来源不明字体不会被打入发布包。
生图与联网搜索是可选能力：未配置时仍可生成，页面会给出一次非阻塞提示，Skill 按现有能力降级。

局域网访问：

```bash
./start.sh --language zh --host 0.0.0.0 --port 8001
```

### 3.3 Windows Docker Desktop

PowerShell 中执行：

```powershell
Copy-Item .env.example .env
$env:STUDIO_LANGUAGE = "zh"
docker compose up --build -d
docker compose logs -f sensenova-present
```

打开 `http://127.0.0.1:8001`。停止服务：

```powershell
docker compose down
```

不要执行 `docker compose down -v`，它会连同历史数据卷一起删除。

### 3.4 Windows 原生启动（仅适合界面调试）

```powershell
.\start.ps1 -Language zh -HostAddress 127.0.0.1 -Port 8001
```

也可以运行 `start.bat -Language zh`。完整生成请切换到 Docker Desktop 或 WSL2。

## 4. 启动参数

| 参数 | 默认 | 说明 |
|---|---:|---|
| `--language {zh,en}` | `zh` | 初始界面语言 |
| `--host HOST` | `127.0.0.1` | 监听地址 |
| `--port PORT` | `8001` | 监听端口 |
| `--edition {v1,full}` | `v1` | 发布包请保持 `v1` |
| `--env-file PATH` | `.env` | 指定配置文件 |
| `--reload` | 关闭 | 开发热重载 |
| `--no-install` | 关闭 | 复用已有虚拟环境 |
| `--no-browser-install` | 关闭 | 跳过 Chromium 安装 |
| `--check` | 关闭 | 脱敏输出有效配置，不启动服务 |

配置优先级：**命令行参数 > Shell 环境变量 > `.env` > 内置默认值**。

部署前检查：

```bash
./start.sh --check
```

英文界面：

```bash
./start.sh --language en
```

## 5. 环境变量快速注入

不创建 `.env` 也可以直接从 Shell、CI/CD 或 Agent 注入：

```bash
export SENSENOVA_MODEL_BASE_URL="http://model-host:8000/v1"
export SENSENOVA_MODEL_NAME="your-multimodal-model"
export SENSENOVA_MODEL_API_KEY="EMPTY"
export SENSENOVA_MODEL_DISPLAY_NAME="My multimodal model"
./start.sh --language zh
```

发布版会自动选择：

```dotenv
PPTAGENT_PUBLIC_SKILL_KEYS=sn-ppt-web
PPTAGENT_DEFAULT_SKILL=sn-ppt-web
```

一般无需设置 Skill 路径；启动器会自动发现
`bundled/static-ppt-skill-suite`。维护外置版本时才覆盖
`PPTAGENT_SN_PPT_WEB_SUITE_ROOT`。

## 6. 数据、升级与备份

默认数据目录：`studio/data/`，包括 SQLite、任务、上传文件和生成工作区。可配置：

```dotenv
STUDIO_DATA_DIR=/absolute/path/to/sensenova-present-data
```

升级流程：

1. 停止旧 WebUI 进程；
2. 完整备份 `STUDIO_DATA_DIR`；
3. 解压新版本到新目录；
4. 复用原 `.env`，并把 `STUDIO_DATA_DIR` 指向原数据目录；
5. `./start.sh --check`；
6. 启动新版本并检查历史记录。

不要让两个 WebUI 进程同时写同一个 SQLite 数据目录。多入口场景应反向代理到同一个服务。

## 7. 验证安装

```bash
curl http://127.0.0.1:8001/healthz
```

预期返回：

```json
{"ok": true}
```

然后创建一个 2–3 页 Smoke Case，确认：

- 首页只显示静态演示；
- Skill 在界面显示为 sn-ppt-web，内部 WebUI 键为 `sn-ppt-web`；Harness 根据 query 语言读取 `sn-ppt-web-zh` 或 `sn-ppt-web-en`；
- 生成过程可见；
- 页面预览、播放、讲稿和导出可用；
- 导出包含 `present.html` 及所需资源。

## 8. 安全说明

- `.env` 已被 Git 和 Docker 构建上下文忽略。
- 不要把真实密钥写入 README、Shell 脚本或镜像。
- `--host 0.0.0.0` 会向网络暴露服务；请在反向代理、防火墙或 VPN 后使用。
- V1 无登录鉴权，不应直接暴露到不受信任的公网。
- 对上传材料、搜索图片和生成内容的使用与分发权限由部署者负责。

## 9. 更多文档

- [完整配置项](docs/CONFIGURATION.md)
- [生产部署与反向代理](docs/DEPLOYMENT.md)
- [常见故障处理](docs/TROUBLESHOOTING.md)
- [第三方依赖说明](THIRD_PARTY_NOTICES.md)
