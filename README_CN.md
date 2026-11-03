# gemini-web2api

<p align="center">
  <img src="logo.png" width="200" alt="gemini-web2api logo">
</p>

[English](README.md)

将 Google Gemini 网页端转换为 OpenAI 兼容 API. 零成本, 跨平台, 单文件.

## 特性

- **可选密钥**: `api_keys` 为空时免密, 填入密钥后按 OpenAI Bearer Key 校验
- **OpenAI 兼容**: 直接替换 `/v1/chat/completions` 和 `/v1/models`
- **工具调用**: 完整的 Function Calling 支持 (OpenAI 格式)
- **多模型**: Flash (3.6), 扩展思考 (2万字+输出), Pro, Auto, Lite
- **思考深度**: 通过 `@think=N` 后缀调节 (0=最深, 4=最浅)
- **联网搜索**: 内置互联网访问 (Gemini 原生搜索能力)
- **跨平台**: 纯 Python, 仅一个可选依赖 (`httpx` 用于流式输出)
- **流式输出**: 基于 `httpx` 的 SSE Streaming 支持
- **Codex CLI**: Responses API (`/v1/responses`) 兼容 OpenAI Codex
- **Gemini CLI**: Google 原生 API (`/v1beta/models`) 兼容 Gemini CLI

## 快速开始

```bash
pip install httpx
python gemini_web2api.py
```

服务启动在 `http://localhost:8081/v1`.

## 客户端配置

### Cherry Studio / ChatBox / 任何 OpenAI 兼容客户端

| 字段 | 值 |
|------|-----|
| Base URL | `http://localhost:8081/v1` |
| API Key | `config.json` 中的任意 `api_keys`；未配置时随便填 |
| Model | `gemini-3.5-flash-thinking` |

### curl

```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"你好!"}]}'
```

### OpenAI Python SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="sk-your-key")
resp = client.chat.completions.create(
    model="gemini-3.5-flash-thinking",
    messages=[{"role": "user", "content": "解释量子计算"}]
)
print(resp.choices[0].message.content)
```

### Gemini CLI

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://localhost:8081
gemini
```

支持 Google 原生 API 端点:
- `GET /v1beta/models` — 模型列表
- `POST /v1beta/models/{model}:generateContent` — 非流式生成
- `POST /v1beta/models/{model}:streamGenerateContent` — 流式生成 (SSE)

## 可用模型

| 模型 | 说明 | 输出量 |
|------|------|--------|
| `gemini-3.6-flash` | 全能模型 (最新) | ~1.2万字 |
| `gemini-3.5-flash` | gemini-3.6-flash 别名 | ~1.2万字 |
| `gemini-3.5-flash-thinking` | 扩展思考, 最长输出 | **~2万字** |
| `gemini-3.5-flash-thinking-lite` | 自适应思考深度 | ~1.5万字 |
| `gemini-3.1-pro` | 高级数学与代码 (需 cookie) | ~1.2万字 |
| `gemini-auto` | 自动选择模型 | 不定 |
| `gemini-flash-lite` | 最快响应, 轻量 | ~1万字 |

### 思考深度

在模型名后追加 `@think=N`:

```
gemini-3.5-flash-thinking@think=0   # 最深 (默认)
gemini-3.5-flash-thinking@think=2   # 中等
gemini-3.5-flash-thinking@think=4   # 最浅
```

## 可选: Cookie 配置 (Pro 模型)

匿名访问对所有模型有效, 但 `gemini-3.1-pro` 在无认证时会路由到 Flash. 要获得真正的 Pro 路由, 需要 **Gemini Advanced (付费订阅)** 账号的 cookie:

```bash
python gemini_web2api.py --cookie-file cookie.txt
```

### 如何获取 Cookie

1. 打开 Chrome, 访问 [gemini.google.com](https://gemini.google.com) 并登录 **Gemini Advanced** 付费账号
2. 打开开发者工具 (F12) → Application → Cookies → `https://gemini.google.com`
3. 复制以下 cookie 值: `SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-1PSID`
4. 创建 `cookie.txt`, 格式如下:

```
SID=你的SID值; HSID=你的HSID值; SSID=你的SSID值; APISID=你的APISID值; SAPISID=你的SAPISID值; __Secure-1PSID=你的1PSID值
```

或使用 JSON 格式:
```json
{"cookie": "SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx", "sapisid": "你的SAPISID值"}
```

**替代方案 (浏览器扩展)**: 本项目自带 `gemini-cookie-sync-extension` 扩展, 可一键导出 `gemini-auth.json`, 并支持定时自动同步 (见下节).

### 登录账号路径与 XSRF Token

如果已登录的 Gemini 页面 URL 带账号序号, 例如:

```
https://gemini.google.com/u/1/app/...
```

请把 `auth_user` 设置为该序号。登录态的 Gemini Web 请求还可能需要页面里的 XSRF token。该 token 在渲染后的 Gemini 页面源码中名为 `SNlM0e`; 在 `config.json` 中填入 `xsrf_token` 后, 服务会把它作为 `at` 表单字段提交。

示例:

```json
{
  "cookie_file": "/app/cookie.txt",
  "auth_user": "1",
  "xsrf_token": "AOOh0P...",
  "gemini_bl": "boq_assistant-bard-web-server_YYYYMMDD.xx_p0"
}
```

如果登录态请求返回 HTTP 400 且错误中包含 `xsrf`, 请刷新 Gemini Web 后更新 `xsrf_token`, 并确认 `auth_user` 与浏览器 URL 中的 `/u/<序号>/` 一致.

Pro 路由需要 **Gemini Advanced** (付费订阅). 免费 Google 账号的 cookie 可以登录认证, 但会静默回退到 Flash.

## 自动同步 Cookie (Chrome 扩展)

`gemini-cookie-sync-extension` 目录是一个未打包的 Chrome 扩展 (Manifest V3). 它既能手动导出 `gemini-auth.json`, 也能**定时把最新会话推送到正在运行的服务**, 由服务端自动写入 `gemini-auth.json` 并更新 `config.json`.

服务端接口:

```
POST /v1/auth/sync
Content-Type: application/json
X-Sync-Token: <可选, 仅当 config.json 设置了 sync_token 时>

{
  "cookie": "SAPISID=...; __Secure-1PSID=...",
  "sapisid": "...",
  "auth_user": "0",
  "xsrf_token": "AOOh0P...",
  "gemini_bl": "boq_assistant-bard-web-server_YYYYMMDD.xx_p0"
}
```

成功后返回:

```json
{
  "ok": true,
  "auth_file": "D:\\gitcode\\gemini-web2api\\gemini-auth.json",
  "config_file": "D:\\gitcode\\gemini-web2api\\config.json",
  "updates": {"cookie_file": "...", "auth_user": "0", "xsrf_token": "...", "gemini_bl": "..."},
  "saved_at": 1792301287
}
```

服务会自动改写 `config.json` 中这些字段: `cookie_file` (指向 `gemini-auth.json` 的绝对路径)、`auth_user`、`xsrf_token`, 以及请求中带上的 `gemini_bl`. 其它字段保持不变, 并且**内存中的配置会立即生效, 无需重启**.

### 安装与使用

1. Chrome 打开 `chrome://extensions`, 开启右上角"开发者模式"
2. 点击"加载已解压的扩展程序", 选择 `gemini-cookie-sync-extension` 目录
3. 打开 [gemini.google.com](https://gemini.google.com) 并登录, 刷新页面
4. 点扩展图标, 在 **Server URL** 填入服务地址 (默认 `http://127.0.0.1:8081`, 需与 `config.json` 的 `port` 一致)
5. 点 **Sync to server now** 立即同步; 勾选 **Sync automatically in the background** 后可按设定的分钟数定时同步
6. **Export gemini-auth.json (manual)** 仍保留, 用于不便联网或想手动放置文件的场景

### 安全说明

- 接口不是模型接口, 因此**不受 `api_keys` 限制**; 若要防止本机其它程序写入, 可在 `config.json` 中设置 `sync_token`, 并在扩展里填同样的值 (扩展会以 `X-Sync-Token` 头发送).
- 服务会拒绝 `Origin` 头存在且不是 `chrome-extension://` 的请求, 以避免恶意网页通过浏览器向本机服务注入 cookie.
- 定时同步需要有一个已登录的 Gemini 标签页 (或页面抓取成功) 才能拿到最新的 XSRF; 若都拿不到, 同步会失败并提示, cookie 与 XSRF 均不会被写入.
- 扩展与 `gemini-auth.json` 都包含真实的 Google 会话凭据, 请勿分享或提交到 Git (`gemini-auth.json`、`config.json` 已在 `.gitignore` 中).

## 配置文件

在同目录创建 `config.json`:

```json
{
  "port": 8081,
  "host": "0.0.0.0",
  "retry_attempts": 3,
  "retry_delay_sec": 2,
  "request_timeout_sec": 180,
  "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
  "auth_user": null,
  "xsrf_token": null,
  "api_keys": ["sk-your-key"],
  "cookie_file": null,
  "proxy": null,
  "log_requests": true,
  "temporary_chats": false,
  "sync_token": null
}
```

将 `temporary_chats` 设置为 `true` 后，请求会使用 Gemini 网页版的临时聊天，
不会将对话保存在账号历史记录中。

`api_keys` 为空数组 `[]` 时不校验密钥；填入一个或多个密钥后, `/v1/*` 接口需要 `Authorization: Bearer <key>` 或 `x-api-key: <key>`.

`sync_token` 用于保护 Chrome 扩展调用的 `/v1/auth/sync` 接口 (它不校验 `api_keys`). 留空则不校验, 仅依赖 `Origin` 检查.

## Docker 部署

```bash
cp config.example.json config.json
docker build -t gemini-web2api .
docker run -d --name gemini-web2api -p 8081:8081 -v ./config.json:/app/config.json gemini-web2api
```

或使用 Docker Compose:

```bash
cp config.example.json config.json
docker compose up -d
```

如需挂载 Cookie 文件:

```bash
docker run -d --name gemini-web2api -p 8081:8081 -v ./config.json:/app/config.json -v ./cookie.txt:/app/cookie.txt gemini-web2api
```

此时 `config.json` 中设置 `"cookie_file": "/app/cookie.txt"`.

> **注意**: 如果 Docker 默认 bridge 网络下出现空回复 (`content: null`), 请切换到 host 网络: `docker run --network host ...` 或在 compose 文件中添加 `network_mode: host`. 这是 Gemini 上游拒绝来自 Docker NAT IP 段的请求导致的.

## 代理配置

如果无法直接访问 `gemini.google.com` (连接超时), 需要配置代理:

**方式 1: 命令行参数**
```bash
python gemini_web2api.py --proxy http://127.0.0.1:7890
```

**方式 2: config.json**
```json
{"proxy": "http://127.0.0.1:7890"}
```

**方式 3: 环境变量** (自动检测)
```bash
set HTTPS_PROXY=http://127.0.0.1:7890
python gemini_web2api.py
```

支持 Clash, V2Ray, Shadowsocks 等任何 HTTP 代理.

## 图片输入

Chat Completions 和 Responses API 支持 OpenAI 风格的多模态消息。图片可以使用
HTTP(S) URL 或 base64 data URL:

```python
resp = client.chat.completions.create(
    model="gemini-3.6-flash",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
        ]
    }]
)
```

## 图片生成

Gemini 网页端的图片生成由 **提示词** 触发, 不需要切换模型: 只要 prompt 是生成类指令
(例如 "生成一张...图片" / "Generate an image of ..."), Gemini 会自动出图。新增 OpenAI
兼容端点:

```
POST /v1/images/generations
```

请求:

```json
{
  "model": "gemini-3.6-flash",
  "prompt": "生成一张红苹果放在木桌上的图片",
  "response_format": "url"
}
```

首次生成不需要传 `session_id`，响应会返回 Gemini 网页端的会话 ID：

```json
{
  "created": 1700000000,
  "data": [{ "url": "https://lh3.googleusercontent.com/gg-dl/..." }],
  "session_id": "c_5d0fb2ff1d694a5d"
}
```

后续要继续同一组图片生成时，把上一步返回的 `session_id` 原样传回：

```json
{
  "model": "gemini-3.6-flash",
  "prompt": "保持构图不变，把苹果换成绿色",
  "session_id": "c_5d0fb2ff1d694a5d",
  "response_format": "url"
}
```

- `response_format` 支持 `url` (默认) 与 `b64_json`。
- `session_id` 使用 Gemini 网页端会话 ID 格式 (`c_` + 16 位十六进制)，与网页 URL `/app/<cid>` 中的 ID 一致。服务端会把它和本轮 `rid` 一起作为会话 metadata 续接，行为与网页连续对话一致。
- 不传 `session_id` 时创建新会话；传未知或已过期的 ID 返回 `404`，格式不合法返回 `400`。会话状态保存在当前服务进程内，闲置 1 小时或服务重启后失效。
- Chat Completions / Responses 中如果 Gemini 出图, 回复文本末尾会自动附加
  `![image](...)` Markdown 链接。
- 若该提示词触发的是"图片搜索"而非"图片生成", 响应会返回上游说明文本并以 HTTP 502 结束。
- 明确要求生成视频/动画的 prompt 会直接返回 `400`, `error.code` 为 `video_not_supported`, 不会调用 Gemini。
- 如果 Gemini 仍意外返回视频, 服务会自动追加"只返回一张静态图片"的约束重试一次; 重试后仍是视频则返回 `502`,
  `error.code` 为 `gemini_video_response`, 并带 `media_type` (如 `video/mp4`)。
- 其它图片生成失败也会返回稳定的错误码: `gemini_text_response` (上游只返回文本)、
  `gemini_empty_response` (无图也无文本)、`gemini_upstream_error` (上游异常)。
  错误对象同时包含 `type` 和 `code`, 便于客户端按类型处理, 例如:

```json
{
  "error": {
    "message": "Gemini returned a video (video/mp4) instead of an image. ...",
    "type": "upstream_unsupported_media",
    "code": "gemini_video_response",
    "media_type": "video/mp4"
  }
}
```

> **注意**: 生成图片的 `lh3.googleusercontent.com/gg-dl/...` 链接受 Google 会话保护,
> 服务端直接下载通常会返回 `403`。`b64_json` 模式下若下载失败会自动回退为返回 `url`;
> 该 URL 在你的已登录浏览器中可正常打开。

## 已知限制

- **图片上传可能需要 Cookie**: 多模态输入使用 Gemini 网页端图片上传接口。匿名上传失败时, 请配置 Gemini cookie。
- **图片生成可能被限流**: Google 可能临时拒绝出图 ("can't create any for you right now"), 此时 `/v1/images/generations` 返回 502。
- **Pro/Ultra 非真实路由**: 无付费订阅 cookie 时, `gemini-3.1-pro` 实际路由到 Flash 模型. "Pro" 只是 UI 偏好标签.
- **单轮对话**: Chat Completions 每次请求仍是独立对话, 多轮上下文通过在 prompt 中包含历史消息模拟。图片生成接口可通过复用 `session_id` 接续同一组图像生成会话.
- **频率限制**: Google 可能限制高频请求, server 会自动重试但持续高负载可能被封.

## 系统要求

- Python 3.8+
- `httpx` (`pip install httpx`) — 用于流式请求
- 需要能访问 `gemini.google.com` (部分地区需代理)

## 工作原理

逆向 Google Gemini 网页端的 StreamGenerate 协议, 将 OpenAI API 格式与 Gemini 内部 protobuf-like 格式互转. 模型选择通过请求 payload 的 `[79]` 字段控制, 映射自 Gemini 前端 JS 源码中的 `MODE_CATEGORY` 枚举.

## 致谢

- [linux.do](https://linux.do) 社区
- 开源 API 代理生态

## License

MIT
