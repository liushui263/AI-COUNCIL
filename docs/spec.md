# AI 议事厅（AI Debate Sandbox）— 开发规格文档

> 版本：v1.0（2026-09-27）
> 目标读者：负责实现的 AI 编程助手（Claude Code）+ 项目 owner（阅读中文部分即可）

## 0. 如何使用本文档

把下面这段话连本文档一起发给 Claude Code：

> 请阅读并实现这份《AI 议事厅开发规格文档》。按"14. 建议实施步骤"的顺序开发；遇到规格不清楚的地方，先按合理判断实现并在代码注释中标注 `TODO(规格待确认)`，不要停下来反问。全部完成后，对照"13. 验收标准"逐项自检。

---

## 1. 产品概述

**一句话**：用户输入一个主题，应用拉多个 AI 进来辩论若干轮，最后输出一份标出共识与分歧的综合报告。

**解决的问题**：重要决策（买什么、选什么方案、怎么规划）想听多角度意见时，不用在各个 AI 应用之间手动复制粘贴。

**运行形态**：在用户自己电脑（Mac）上本地运行的小型 web 应用。API Key 只存本机，不经过任何第三方服务器。

**典型使用场景**：
- 购物决策（如：两款冰柜选哪个，价格/能耗/售后一次辩清楚）
- 技术方案评审（家庭网络改造、设备选购，让几个 AI 互相挑刺）
- 旅行规划（行程、酒店多选一）
- 通用：任何"想听不同角度"的问题——学习、写作、方案评审

---

## 2. 功能需求

### 2.1 新建辩论
- **话题输入**：必填，单行或多行文本。
- **背景材料**（可选）：多行文本框，用户可粘贴补充信息（如价格对比表、现状描述），会原样发给每个参会 AI。
- **参会者选择**：勾选 2~8 个 AI。每个参会者可配置：
  - Provider（见第 5 节）
  - Model：下拉选择（从该 provider 的模型列表动态加载，见 2.5）+ 允许手动输入
  - 角色（可选）：如"成本派""质量派""风险派"；留空则为"中立专家"
- **轮数定制**：
  - 盲写首轮：开关，默认开
  - 交叉辩论轮数：数字选择器，0~5，默认 2
- **综合者选择**：指定由哪个参会者（provider+model）执行最后的综合，默认=第一个参会者。
- **高级参数**（可折叠）：max_tokens（默认 1500，范围 200~8000）、temperature（默认 0.7，范围 0~2）。

### 2.2 实时观看辩论
- 点击"开始辩论"后进入进行中视图，按轮次流式显示每个 AI 的发言。
- 显示状态：等待中 / 生成中 / 已完成 / 失败（含错误原因）。
- 允许中途取消（取消后保留已完成轮次，可查看、可综合）。

### 2.3 综合报告
- 辩论结束后（或手动触发）生成，Markdown 格式，包含四个固定章节：
  - `## 共识`
  - `## 未解决的分歧`
  - `## 各方保留意见`
  - `## 最终建议`
- 要求：忠实原文不虚构；分歧点注明是哪几方的观点冲突。

### 2.4 历史存档
- 每次辩论自动存档：话题、配置、各轮完整发言、综合报告、用 token 统计。
- 历史列表支持：查看、导出 Markdown、删除。

### 2.5 Provider 与 Key 管理（设置页）
- Provider 列表展示：名称、是否需要 key、本机是否已配置 key、免费档说明、key 获取链接。
- Key 增删改：输入框保存到本机 `.env`；前端永远只能看到"已配置/未配置"，看不到 key 明文。
- **模型列表动态加载**：每个 provider 提供"刷新模型列表"按钮，调用其 `/models` 类接口（见第 5 节），结果缓存到 `data/config.json`；新建辩论时的模型下拉框从此读取。

---

## 3. 非功能需求

- **零依赖**：后端只用 Python 3.9+ 标准库（`http.server`、`urllib`、`json`、`concurrent.futures` 等），用户无需 `pip install`。
- **单文件前端**：`frontend/index.html` 一个文件搞定（内联 CSS/JS），不依赖 CDN，断网也能打开页面（调 API 时除外）。
- **本地绑定**：只监听 `127.0.0.1:8000`。
- **Key 安全**：key 只存本机 `.env`；任何 API 响应、日志、前端都不输出 key 明文；`.env` 和 `data/` 加入 `.gitignore`。
- **单场辩论成本可控**：默认参数下一场 3 个 AI ×（1 盲写 + 2 辩论）+ 1 次综合 ≈ 10 次调用；用免费档约等于 0 成本。
- **语言**：UI 中文；发给 AI 的提示词用中文（见第 6 节模板）。

---

## 4. 技术架构

```
ai-council/
├── app.py                 # 入口：python3 app.py → 启动服务
├── backend/
│   ├── server.py          # HTTP 路由（基于 http.server）
│   ├── debate.py          # 辩论引擎：轮次编排、并发调用、综合
│   ├── providers.py       # 各家 API 适配器（统一 chat 接口）
│   └── store.py           # JSON 存储、.env 读写、usage 统计
├── frontend/
│   └── index.html         # 单文件前端
├── data/                  # debates/*.json、config.json（gitignore）
├── .env                   # API keys（gitignore）
├── .env.example           # key 名称示例
├── .gitignore
└── README.md              # 运行说明：python3 app.py → 打开 http://127.0.0.1:8000
```

- 并发：轮内多个参会者并行调用（`ThreadPoolExecutor`，`max_workers=min(8, 参会者数)`）；轮次之间串行。
- 实时推送：SSE（`GET /api/debates/{id}/events`），事件见 7.3。
- 流式输出：provider 支持 `stream=true` 时透传 token 增量；不支持则整段返回。前端统一处理两种事件。

---

## 5. Provider 适配器规格

### 5.1 统一接口

```python
def chat(provider_id: str, model: str, system: str, user: str,
         max_tokens: int, temperature: float, stream: bool,
         on_token=None) -> dict:
    """返回 {"text": str, "usage": {"prompt_tokens": int, "completion_tokens": int} | None}"""
```

所有适配器必须实现：超时（连接 10s / 读取 120s）、429 指数退避重试（最多 2 次）、统一异常 `ProviderError(msg, retryable)`。

### 5.2 三类接入方式

**A. OpenAI-compatible**（`POST {base_url}/chat/completions`）
- Header：`Authorization: Bearer {key}`（Pollinations 可无 key）
- Body：`{"model":..., "messages":[{"role":"system",...},{"role":"user",...}], "max_tokens":..., "temperature":..., "stream":...}`
- OpenRouter 建议附加（可选）：`HTTP-Referer`、`X-Title`（应用名，用于流量归属）

**B. Gemini native**（`POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}`）
- Body：`{"systemInstruction":{"parts":[{"text": system}]}, "contents":[{"role":"user","parts":[{"text": user}]}], "generationConfig":{"maxOutputTokens":..., "temperature":...}}`
- 解析：`candidates[0].content.parts[*].text` 拼接；`finishReason != "STOP"` 时在文本末尾标注原因。

**C. Anthropic native**（`POST https://api.anthropic.com/v1/messages`）
- Headers：`x-api-key: {key}`、`anthropic-version: 2023-06-01`、`content-type: application/json`
- Body：`{"model":..., "max_tokens":..., "system":..., "messages":[{"role":"user","content":...}]}`
- 解析：`content` 数组中 `type=="text"` 的 `text` 拼接。

### 5.3 Provider 清单（已核验，2026-09-27）

| # | Provider | 方式 | Endpoint | Key | 费用/免费档 | Key 获取 | 默认模型 |
|---|----------|------|----------|-----|------------|---------|---------|
| 1 | Gemini | B | `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` | 必需（`?key=`） | 免费档：Flash 系列免费，无需绑卡；RPM/RPD 按模型不同（以 AI Studio 显示为准） | [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) | `gemini-2.5-flash` |
| 2 | Groq | A | `https://api.groq.com/openai/v1` | 必需（`gsk_`开头） | 免费档：约 30 RPM + 每日 token 上限（分模型），无需绑卡 | [console.groq.com/keys](https://console.groq.com/keys) | `llama-3.3-70b-versatile` |
| 3 | OpenRouter | A | `https://openrouter.ai/api/v1` | 必需（`sk-or-v1-`开头） | 免费：模型名加 `:free` 后缀；未充值账户 50 次/天、20 次/分钟；一次性充值 $10 起永久解锁 1000 次/天 | [openrouter.ai/keys](https://openrouter.ai/keys) | 动态：从 `/models` 取第一个 `:free` 模型 |
| 4 | Pollinations | A | `https://gen.pollinations.ai/v1/chat/completions`（新；旧 `https://text.pollinations.ai/openai` 保留为备用） | 可选（无 key 可用，有 key 额度更高；在 enter.pollinations.ai 免费领） | 免费 | [enter.pollinations.ai](https://enter.pollinations.ai) | `openai` |
| 5 | DeepSeek | A | `https://api.deepseek.com` | 必需 | 按量付费，极便宜 | [platform.deepseek.com](https://platform.deepseek.com) | `deepseek-chat` |
| 6 | OpenAI | A | `https://api.openai.com/v1` | 必需 | 按量付费 | [platform.openai.com](https://platform.openai.com) | `gpt-4o-mini` |
| 7 | Anthropic | C | `https://api.anthropic.com` | 必需 | 按量付费；**注意：Claude Pro/Claude Code 订阅不含 API 额度，必须单独开 API 账单** | [console.anthropic.com](https://console.anthropic.com) | 以官方为准，默认选 Haiku 级别便宜模型 |
| 8 | xAI | A | `https://api.x.ai/v1` | 必需 | 按量付费 | [console.x.ai](https://console.x.ai) | 以官方为准 |

**防大改设计（重要）**：
- 上表"默认模型"只是初始值；模型名必须做成**用户可编辑的配置项**，不得硬编码死。
- 设置页"刷新模型列表"：A 类调 `{base}/models`，B 类调 `GET /v1beta/models?key={key}`，C 类调 `GET https://api.anthropic.com/v1/models`（header 同 5.2C）；结果缓存到 `data/config.json`。
- 启动辩论前校验：参会者 ≥2、所选 provider 的 key 状态满足要求（需要 key 但未配置 → 阻止启动并提示去设置页）。

---

## 6. 辩论协议（精确算法）

输入：`topic`、`background`（可选）、`participants: [{provider, model, role}]`、`blind_round: bool`、`debate_rounds: int`、`synthesis: {provider, model}`、`max_tokens`、`temperature`。

### 6.1 提示词模板（中文，直接使用）

**盲写轮 system**：
```
你是一位{role}。请针对以下主题给出你的独立分析和具体建议方案。
要求：结构清晰，给出明确结论和理由；不要提及其他 AI（你现在看不到它们的观点）。
```
（role 为空时填"中立客观的专家"）

**盲写轮 user**：
```
主题：{topic}
背景信息：{background} ← 为空时整行省略
请给出你的分析与建议方案。
```

**交叉辩论轮 user**（system 同盲写轮）：
```
主题：{topic}

以下是其他 AI 在上一轮的观点：


{上一轮文本A}


{上一轮文本B}

请针对以上观点：
1) 指出你认同的部分及原因；
2) 指出你反对或认为有漏洞的部分及原因；
3) 给出修正后的最终建议。如无修正必要，说明原因并维持原方案。
```

**综合 system**：`你是一位中立的综合者，只基于给定的辩论记录做综合，不引入外部知识，不虚构。`

**综合 user**：
```
以下是多位 AI 就同一主题辩论的完整记录：

{transcript} ← 格式：文本 …… 文本

请输出一份综合报告（Markdown），必须包含以下四个二级标题：
## 共识
## 未解决的分歧
## 各方保留意见
## 最终建议
要求：分歧点注明是哪几方的观点冲突；最终建议给出倾向性结论及前提条件。
```

### 6.2 执行流程

```
debate = 创建记录(status=running)
if blind_round:
第0轮：所有参会者并行调用 chat(system=盲写system, user=盲写user)
写入 rounds[0]
for r in 1..debate_rounds:
第r轮：每个参会者并行调用 chat(user=交叉辩论user(含其他人上一轮文本))
写入 rounds[r]
若全部失败 → status=partial，中断
综合：调用 synthesis 指定的模型(system=综合system, user=综合user(transcript))
写入 synthesis，status=done（若综合失败 → status=partial，保留各轮）
每个事件实时推送 SSE
```

- 上下文裁剪：交叉轮只带**其他人上一轮**文本（不带全部历史），控制 token。
- 单个参会者失败：该条目标记 `status=failed` + 错误原因，辩论继续；该轮结束后若成功数 <2 且还有轮次，继续（综合时注明缺席方）。
- 取消：前端发 `POST /api/debates/{id}/cancel`，后端置中断 flag，当前轮完成后停止，可手动触发综合。

---

## 7. 后端 API 设计

Base：`http://127.0.0.1:8000`，全部 JSON（除 SSE 与导出）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/providers` | provider 列表：`[{id, name, need_key, key_configured(bool), free_tier_desc, key_url}]`，**绝不返回 key 明文** |
| PUT | `/api/providers/{id}/key` | body `{"key": "..."}` → 写入 `.env` |
| DELETE | `/api/providers/{id}/key` | 删除 key |
| POST | `/api/providers/{id}/models/refresh` | 刷新模型列表 → 缓存到 `data/config.json`，返回列表 |
| POST | `/api/debates` | 创建并启动辩论（body 见 6 输入），返回 `{"debate_id"}`；参数非法/参会者不足/key 缺失 → 400 + 中文错误信息 |
| GET | `/api/debates/{id}/events` | SSE 事件流（见 7.3） |
| POST | `/api/debates/{id}/cancel` | 取消进行中的辩论 |
| POST | `/api/debates/{id}/synthesize` | 对已结束/已取消的辩论（重新）生成综合报告 |
| GET | `/api/debates` | 历史列表（倒序，摘要） |
| GET | `/api/debates/{id}` | 完整记录（含各轮、综合、usage） |
| DELETE | `/api/debates/{id}` | 删除存档 |
| GET | `/api/debates/{id}/export.md` | 下载 Markdown 报告（含各轮发言 + 综合） |

### 7.3 SSE 事件格式

```
event: debate_started data: {"debate_id": "..."}
event: round_started data: {"round": 0}
event: participant_started data: {"round": 0, "participant": "gemini"}
event: token data: {"round": 0, "participant": "gemini", "delta": "..."}
event: message_done data: {"round": 0, "participant": "gemini", "text": "...", "usage": {...}}
event: message_failed data: {"round": 0, "participant": "groq", "error": "429 限流，已重试…"}
event: round_done data: {"round": 0}
event: synthesis_done data: {"text": "..."}
event: debate_done data: {"status": "done|partial"}
event: debate_cancelled data: {}
```

---

## 8. 前端 UI 规格

单文件 `frontend/index.html`，原生 JS + 内联 CSS，无框架、无 CDN。四个视图（顶部 Tab 切换）：

1. **新建辩论**
- 话题输入框（必填）、背景材料文本框（可选）
- 参会者勾选区：每个 provider 一行 → 勾选框 + 模型下拉（动态列表/手动输入）+ 角色输入框（可选）+ key 状态徽标（未配置 key 且必需 → 红色提示"去设置页配置"）
- 轮数：盲写开关（默认开）+ 交叉轮数步进器（0~5，默认 2）
- 综合者下拉（默认第一个参会者）、高级参数折叠（max_tokens、temperature）
- "开始辩论"按钮：参数校验不通过时禁用并说明原因
2. **进行中**：按轮次分组显示发言卡片（provider 名 + 模型 + 角色 + 状态点），流式追加文本；取消按钮；完成后自动跳综合报告
3. **历史**：列表（话题、时间、参会者数、状态）→ 详情（完整轮次 + 综合报告）→ 导出.md / 删除
4. **设置**：provider 列表（key 配置/删除、刷新模型列表、免费档说明、key 获取链接）

样式要求：中性浅色/深色自适应（`prefers-color-scheme`），手机可正常操作。

---

## 9. 数据模型与存储

`data/debates/{debate_id}.json`：
```json
{
"id": "uuid4",
"created_at": "2026-09-27T17:00:00+08:00",
"topic": "...",
"background": "...",
"config": {"participants": [{"provider": "gemini", "model": "gemini-2.5-flash", "role": "成本派"}],
"blind_round": true, "debate_rounds": 2,
"synthesis": {"provider": "gemini", "model": "gemini-2.5-flash"},
"max_tokens": 1500, "temperature": 0.7},
"rounds": [{"index": 0, "messages": [
{"provider": "gemini", "model": "...", "role": "...",
"text": "...", "status": "ok",
"usage": {"prompt_tokens": 123, "completion_tokens": 456}}]}],
"synthesis": {"text": "...", "by": "gemini/gemini-2.5-flash", "status": "ok"},
"status": "done"
}
```

`data/config.json`：模型列表缓存 `{provider_id: {"models": [...], "updated_at":...}}`。
`.env`：`GEMINI_API_KEY=`、`GROQ_API_KEY=`、`OPENROUTER_API_KEY=`、`POLLINATIONS_API_KEY=`、`DEEPSEEK_API_KEY=`、`OPENAI_API_KEY=`、`ANTHROPIC_API_KEY=`、`XAI_API_KEY=`（手写解析.env，不引入 python-dotenv）。

---

## 10. Key 管理与安全

- Key 只存本机 `.env`；`GET /api/providers` 只返回 `key_configured: true/false`。
- 日志中对 key 脱敏（只记前 4 位，如 `AIza…`，或不记）。
- `.env`、`data/` 进 `.gitignore`；提供 `.env.example`（只有 key 名，无值）。
- README 明确：不要把 `.env` 发给任何人；本应用不向除所选 provider 之外的任何地址发请求。

---

## 11. 错误处理与重试

| 情况 | 处理 |
|------|------|
| 429 限流 | 指数退避（1s、4s）重试，最多 2 次；仍失败 → 该条目标记 failed，辩论继续 |
| 401/403 | 标记 failed，错误信息提示"key 无效或额度不足，请检查设置页"；**不再重试** |
| 超时/网络错误 | 重试 1 次；仍失败 → failed |
| 模型名无效（400） | failed，错误信息原样透出 provider 返回的 message |
| 单个参会者全轮失败 | 辩论继续；综合报告中注明该方缺席 |
| 后端重启 | 进行中的辩论标记为 `interrupted`，历史可查，不自动重跑 |

---

## 12. 成本控制

- 默认参数单场 ≈ 10 次 API 调用（3 参会者 × 3 轮 + 1 综合）。
- 每次调用记录 `usage`（provider 返回的 prompt/completion tokens），存档并在历史详情页展示。
- 选择含付费 provider 时，新建页显示黄色提示"本次辩论将产生 API 费用（走你自己的账户）"。
- 免费档默认阵容（设置页可一键勾选）：Gemini + Groq + OpenRouter(`:free`模型)——零成本开箱即用。

---

## 13. 验收标准

- [] `python3 app.py` 直接启动，无需 pip install；浏览器打开 `http://127.0.0.1:8000` 页面正常
- [] 未配置 key 时，Pollinations 可直接建 1 个参会者的辩论并跑通全流程
- [] 配置 2 个以上 provider 后，盲写轮各方发言互不引用（抽查）
- [] 2 轮交叉辩论 + 综合报告完整生成，四章节齐全
- [] 模拟一个参会者失败（如填错模型名），辩论不中断，失败信息清晰展示
- [] 历史存档：重启服务后记录不丢失；导出.md 内容完整
- [] 安全：前端任何接口返回中无 key 明文；日志中无 key 明文
- [] 取消辩论：已完成的轮次保留，可手动触发综合
- [] 模型列表刷新功能可用；模型名改为手动输入时能正常调用

---

## 14. 建议实施步骤

1. `store.py`：.env 解析、JSON 存档、config 缓存
2. `providers.py`：三类适配器（A/B/C）+ 统一 `chat()` + 重试/超时；写 `smoke_test.py`（用 Pollinations 免 key 验证链路）
3. `debate.py`：轮次编排、并发、提示词模板、综合、usage 统计
4. `server.py`：路由 + SSE + 取消 + 导出
5. `frontend/index.html`：四个视图
6. 联调：按验收标准逐项自检，修 bug
7. `README.md` + `.env.example` + `.gitignore`

---

## 附录：Key 获取速查

- Gemini（免费）：https://aistudio.google.com/app/apikey —— 登录 Google 账号 → Create API Key，无需绑卡
- Groq（免费）：https://console.groq.com/keys —— 注册 → Create API Key，无需绑卡
- OpenRouter（免费模型）：https://openrouter.ai/keys —— 注册 → Create Key（`sk-or-v1-`开头），无需绑卡；免费模型列表：https://openrouter.ai/models?max_price=0
- Pollinations（免费/可选）：https://enter.pollinations.ai —— 无 key 也可用，有 key 额度更高
- DeepSeek（极便宜）：https://platform.deepseek.com
- Anthropic（按量付费，注意订阅不通用）：https://console.anthropic.com
- OpenAI（按量付费）：https://platform.openai.com
- xAI（按量付费）：https://console.x.ai
