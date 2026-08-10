# 配置手册

## 配置来源与优先级

`scripts/launch.py` 读取配置的优先级为：

1. 启动命令参数；
2. 当前进程环境变量；
3. `--env-file` 指定的文件，默认项目根目录 `.env`；
4. 内置默认值。

`.env` 使用 `KEY=VALUE`，也接受 `export KEY=VALUE`。值可以加单引号或双引号，当前解析器不做
Shell 变量插值，因此不要写 `${HOME}`，请填写完整路径。

## WebUI

| 变量 | 默认 | 说明 |
|---|---|---|
| `STUDIO_LANGUAGE` | `zh` | `zh` / `en` |
| `SENSE_NOVA_LOCAL_HOST` | `127.0.0.1` | 监听地址 |
| `SENSE_NOVA_LOCAL_PORT` | `8001` | 监听端口 |
| `STUDIO_EDITION` | `v1` | 发布包保持 `v1` |
| `STUDIO_DATA_DIR` | `./studio/data` | 数据库、任务、上传和工作区 |
| `STUDIO_AGENT_MAX_TOKENS` | `40960` | 每轮最大 Token |
| `STUDIO_MAX_PER_MODEL` | `0` | `0` 表示不按模型限制并发 |
| `STUDENT_TEMPERATURE` | `0.3` | 静态生成温度 |

## sn-ppt-web（内部实现：long-horizon-presenter Harness + 双语冻结 Skill）

前端始终只展示一个 `sn-ppt-web`。Harness 根据本轮 query 的主要语言自动选择：中文使用 `sn-ppt-web-zh`，英文使用 `sn-ppt-web-en`。任务快照会记录实际使用的 Skill 名称、语言和目录哈希。

| 变量 | 默认 | 说明 |
|---|---|---|
| `PPTAGENT_PUBLIC_SKILL_KEYS` | `long-horizon-presenter` | 公共 Skill 内部键，前端显示为 sn-ppt-web |
| `PPTAGENT_DEFAULT_SKILL` | `long-horizon-presenter` | 默认 Skill 内部键，前端显示为 sn-ppt-web |
| `PPTAGENT_LONG_HORIZON_PRESENTER_SUITE_ROOT` | 自动发现 | 同时包含 `skills/` 与 `harnesses/` 的目录 |
| `PPTAGENT_LONG_HORIZON_PRESENTER_SKILL_ROOT` | Suite 下默认位置 | 单独覆盖 Skill |
| `PPTAGENT_LONG_HORIZON_PRESENTER_HARNESS_ROOT` | Suite 下默认位置 | 单独覆盖 Harness |

正常安装不要改上述路径。若你维护了新版 Skill/Harness，必须保证两者来自相互匹配的同一套
Suite，并同时替换。

## 主模型

主模型服务需兼容 OpenAI Chat Completions，并支持 Harness 所需的工具调用和多模态输入。
发布版默认不公开源码中的内部测试模型。以下配置会注册一个新的部署模型，而不是覆盖
下拉框中的第一个模型；更多模型可在 WebUI 的「用户 → 模型配置」中增删。

```dotenv
SENSENOVA_MODEL_BASE_URL=http://model-host:8000/v1
SENSENOVA_MODEL_NAME=your-multimodal-model
SENSENOVA_MODEL_API_KEY=EMPTY
SENSENOVA_MODEL_DISPLAY_NAME=My multimodal model
```

`SENSENOVA_MODEL_BASE_URL` 与 `SENSENOVA_MODEL_NAME` 必须同时提供。两者均为空时，
初始模型列表为空，用户需要先在页面中配置模型。`PPTAGENT_PUBLIC_MODEL_KEYS` 默认为
空值；仅维护内部模型注册表的部署者才应把允许公开的 key 以逗号分隔写入其中。

使用真实 Secret 时请把 `.env` 权限限制为当前用户可读：

```bash
chmod 600 .env
```

## 生图服务

支持两种 Provider。默认 `openai_images`，兼容 OpenAI Images API：

```dotenv
SENSENOVA_IMAGE_PROVIDER=openai_images
SENSENOVA_IMAGE_BASE_URL=https://image.example/v1
SENSENOVA_IMAGE_MODEL=gpt-image-2-adobe-2
SENSENOVA_IMAGE_API_KEY=replace-me
```

当前完成实际验证的模型是 `gpt-image-2-adobe-2`。Harness 会调用
`POST {SENSENOVA_IMAGE_BASE_URL}/images/generations`，请求体包含 `model`、`prompt`、
`size` 和 `n: 1`。返回需满足以下任一格式：

```json
{"data":[{"b64_json":"BASE64_IMAGE_BYTES"}]}
```

或：

```json
{"data":[{"url":"https://downloadable.example/image.png"}]}
```

客户端不会强制发送 `response_format`。URL 模式下，运行 WebUI 的机器必须能直接下载该 URL。
未配置时，Image Agent 仍可使用附件或搜索素材，但无法调用生图服务。

SenseNova U1 使用原生 Images API：

```dotenv
SENSENOVA_IMAGE_PROVIDER=sensenova_u1
SENSENOVA_IMAGE_BASE_URL=https://token.sensenova.cn/v1
SENSENOVA_IMAGE_MODEL=sensenova-u1-fast
SENSENOVA_IMAGE_API_KEY=replace-me
```

Harness 会调用 `POST {SENSENOVA_IMAGE_BASE_URL}/images/generations`，发送 `model`、`prompt`、
U1 支持的固定尺寸桶、`response_format=url` 与 `output_format=png`。推荐模型名为
`sensenova-u1-fast`，也可按账号实际开放型号修改。标准返回为 `data[].url`；客户端也兼容
图片 URL、data URL/base64、`message.images` 和多模态 content block。若填写的是完整
`/images/generations` 地址，Harness 不会重复拼接路径。

## 搜索服务

当前 Harness 使用 Serper 兼容接口：

```dotenv
SENSENOVA_SEARCH_BASE_URL=https://google.serper.dev
SENSENOVA_SEARCH_API_KEY=replace-me
```

接口为 `POST {base_url}/search`，使用 `X-API-KEY` 请求头。当前解析 Serper 的
`organic[]`（`title`、`link`、`snippet`）和 `images[]`（`title`、`imageUrl`/`link`）。
其他字段结构需要单独编写适配器。

## Anthropic 兼容服务与可选模型

```dotenv
SENSENOVA_ANTHROPIC_BASE_URL=https://your-anthropic-compatible-host
SENSENOVA_ANTHROPIC_API_KEY=replace-me
SENSENOVA_GPT56_API_KEY=replace-me
SENSENOVA_KIMI_K3_API_KEY=replace-me
```

不使用对应模型时无需填写。

## 字体与 Chromium

| 变量 | 说明 |
|---|---|
| `PLAYWRIGHT_BROWSERS_PATH` | Chromium 下载与查找目录 |
| `PPT_SKILL_BROWSER_EXE` | 显式指定 Chromium / chrome-headless-shell |
| `STUDIO_DATA_DIR` | 用户授权上传的字体配置与生成资产也写入这里 |

发布包不会附带商业字体。sn-ppt-web（`long-horizon-presenter`）会按用户配置和系统字体进行匹配，并在交付阶段
按实际使用情况打包允许嵌入的字体资源。

## 数据目录建议

本机体验可以使用默认目录；生产部署应使用独立持久目录：

```dotenv
STUDIO_DATA_DIR=/srv/sensenova-present/data
```

Docker Compose 自动使用命名卷 `/data`。SQLite 只能由一个服务进程负责写入。
