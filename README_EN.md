# SenseNova Present WebUI

SenseNova Present WebUI is a self-hosted AI presentation workspace. The public V1 release focuses on static HTML presentations and exposes one `sn-ppt-web` workflow. Its `long-horizon-presenter` Harness detects the query language and selects the frozen `sn-ppt-web-zh` or `sn-ppt-web-en` Skill package.

The primary documentation is available in [Chinese](README.md). This page provides a concise English setup guide.

## Highlights

- Generate a complete static HTML presentation from a prompt and attachments.
- Inspect planning, research, image preparation, page generation, and visual review progress in the WebUI.
- Preview and play slides, inspect speaker notes, and export portable HTML deliverables.
- Configure an OpenAI-compatible multimodal model from `.env` or the WebUI.
- Optionally connect image-generation and Serper-compatible search services.
- Use the fully tested macOS path, or the provided Linux, WSL2, and Windows Docker Desktop launch paths.
- Install 42 bundled OFL/open-source presentation fonts without network access.

> V1 runs without authentication and stores data locally. Do not expose it directly to an untrusted public network.

## Currently verified scope

This V1 release distinguishes implemented compatibility from combinations that
have actually completed end-to-end regression testing:

| Area | Verified today | Other implementations |
|---|---|---|
| Operating system | **macOS**: install, launch, generation, preview, and export | Linux, WSL2, and Windows Docker Desktop launch paths are provided but have not yet received equivalent full regression coverage |
| Main model | A **SenseNova OpenAI-compatible multimodal model**, with Chat Completions, tool calling, and multimodal input | Other OpenAI-compatible models can be configured but are not automatically considered verified |
| Image generation | **OpenAI Images-compatible** (`openai_images`, verified with `gpt-image-2-adobe-2`) and **SenseNova U1** (`sensenova_u1`) providers | U1 uses the native SenseNova Images API; other image models have not been validated individually, and image generation is optional |
| Image search | A **Serper-compatible search endpoint** (`google.serper.dev`) | Other search APIs are not guaranteed to work by changing only the URL; search is optional |

For combinations outside this table, run a 2–3 slide smoke case before production use.

### API response contracts

- **Main model:** non-streaming OpenAI-compatible `POST /chat/completions`; the response must expose `choices[0].message` and `finish_reason`. Reliable generation requires standard OpenAI `tools`/`message.tool_calls` support and multimodal `image_url` blocks containing base64 data URLs. Optional thinking content may be returned as `message.reasoning` or `message.reasoning_content`.
- **Image model:** `openai_images` calls `POST /images/generations` with `model`, `prompt`, `size`, and `n: 1`; the verified model is `gpt-image-2-adobe-2`. `sensenova_u1` calls the native SenseNova Images API with a supported 1K size bucket, `response_format=url`, and `output_format=png`; the recommended model is `sensenova-u1-fast`. Responses must expose either `data[0].b64_json` or a server-downloadable `data[0].url`.
- **Search:** Serper-compatible `POST /search` with `X-API-KEY`; text results come from `organic[]` (`title`, `link`, `snippet`) and image results from `images[]` (`title`, `imageUrl`, with `link` as fallback).

An endpoint being reachable is not sufficient: models or services with different tool-call, multimodal, or response schemas require an adapter.

## Requirements

- Python 3.12
- 8 GB RAM minimum; 16 GB or more recommended
- Network access during the first run for Python packages and Playwright Chromium
- Access to an OpenAI-compatible multimodal model endpoint

## Five-minute setup

Copy the environment template:

```bash
cp .env.example .env
```

Configure at least one model in `.env`:

```dotenv
SENSENOVA_MODEL_BASE_URL=http://model-host:8000/v1
SENSENOVA_MODEL_NAME=your-multimodal-model
SENSENOVA_MODEL_API_KEY=EMPTY
SENSENOVA_MODEL_DISPLAY_NAME=My multimodal model
```

### macOS, Linux, or WSL2

```bash
chmod +x start.sh
./start.sh --language en
```

Open <http://127.0.0.1:8001>.

To listen on the local network:

```bash
./start.sh --language en --host 0.0.0.0 --port 8001
```

### Windows with Docker Desktop

```powershell
Copy-Item .env.example .env
$env:STUDIO_LANGUAGE = "en"
docker compose up --build -d
docker compose logs -f sensenova-present
```

Open <http://127.0.0.1:8001>. Stop the service with:

```powershell
docker compose down
```

Do not run `docker compose down -v` unless you intend to delete the persisted history and generated files.

For native Windows UI debugging, use an explicit English startup language:

```powershell
.\start.ps1 -Language en -HostAddress 127.0.0.1 -Port 8001
```

Full deck generation on Windows should use Docker Desktop or WSL2.

## Optional services

Image generation:

```dotenv
SENSENOVA_IMAGE_PROVIDER=openai_images
SENSENOVA_IMAGE_BASE_URL=https://image.example/v1
SENSENOVA_IMAGE_MODEL=gpt-image-2-adobe-2
SENSENOVA_IMAGE_API_KEY=replace-me
```

SenseNova U1 can be selected as a separate provider:

```dotenv
SENSENOVA_IMAGE_PROVIDER=sensenova_u1
SENSENOVA_IMAGE_BASE_URL=https://token.sensenova.cn/v1
SENSENOVA_IMAGE_MODEL=sensenova-u1-fast
SENSENOVA_IMAGE_API_KEY=replace-me
```

This provider calls `POST /images/generations` with SenseNova's supported size
buckets and downloads the returned `data[].url` image.

Search:

```dotenv
SENSENOVA_SEARCH_BASE_URL=https://google.serper.dev
SENSENOVA_SEARCH_API_KEY=replace-me
```

The application remains usable without these optional services and will surface a non-blocking configuration hint.

## Health check

```bash
curl http://127.0.0.1:8001/healthz
```

Expected response:

```json
{"ok": true}
```

## Data and upgrades

Runtime data is stored in `studio/data/` by default. Override it with:

```dotenv
STUDIO_DATA_DIR=/absolute/path/to/sensenova-present-data
```

Back up that directory before upgrading. Never run two WebUI processes against the same SQLite data directory; route multiple entry points to one service instead.

## Documentation

- [Configuration](docs/CONFIGURATION.md) (Chinese)
- [Deployment](docs/DEPLOYMENT.md) (Chinese)
- [Troubleshooting](docs/TROUBLESHOOTING.md) (Chinese)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
