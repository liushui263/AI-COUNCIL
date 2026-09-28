# AI 议事厅（AI Debate Sandbox）

输入一个主题，让多个 AI 辩论几轮，最后输出一份标出**共识与分歧**的综合报告。
在你自己的电脑上本地运行，API Key 只存本机 `.env`。完整规格见 [docs/spec.md](docs/spec.md)。

## 运行

需要 Python 3.9 或更高版本，**不用 pip install 任何东西**。

```bash
cd ai-council
python3 app.py          # Windows：python app.py
```

启动后会自动打开浏览器；也可以手动访问 <http://127.0.0.1:8000>。
可选参数：`--port 8080` 换端口，`--no-browser` 不自动开浏览器，`--debug` 输出调试日志。

## 第一次使用

1. 什么 key 都不配也能用：在“新建辩论”页点 **一键勾选免费阵容**，会用免 key 的 Pollinations 以不同角色组队。
2. 想要更好的效果，去“设置”页填几个免费 key（都不用绑卡）：
   - Gemini：<https://aistudio.google.com/app/apikey>
   - Groq：<https://console.groq.com/keys>
   - OpenRouter：<https://openrouter.ai/keys>（用 `:free` 结尾的模型）
3. 在“设置”页点 **刷新模型列表**，新建辩论时模型下拉框就有完整列表；模型名也可以直接手动输入。

付费 provider（DeepSeek、OpenAI、Anthropic、xAI）费用走你自己的账户，选中时页面会有黄色提示。
注意：Claude Pro / Claude Code 订阅不含 API 额度，Anthropic 需要单独开 API 账单。

## 安全

- key 只保存在本目录的 `.env` 文件里；页面和所有接口只显示“已配置/未配置”，日志里不记录 key。
- **不要把 `.env` 发给任何人**。`.env` 和 `data/` 已在 `.gitignore` 里。
- 服务只监听 `127.0.0.1`，并拒绝来自其他网站的跨站请求。
- 本应用除了你选择的 provider 之外，不向任何地址发请求。

## 目录

```
ai-council/
├── app.py                 # 入口
├── backend/
│   ├── server.py          # HTTP 路由 + SSE
│   ├── debate.py          # 辩论引擎：轮次编排、并发、提示词、综合
│   ├── providers.py       # 8 家 provider 适配器（统一 chat 接口）
│   └── store.py           # .env、JSON 存档、模型列表缓存
├── frontend/index.html    # 单文件前端（无框架、无 CDN）
├── data/                  # 辩论存档与模型缓存（自动生成）
├── smoke_test.py          # 真实调用一次 Pollinations，验证网络链路
└── tests/                 # 离线端到端测试（用本地假 provider）
```

## 测试

```bash
python3 -m unittest discover -s tests -t . -v   # 离线测试，不访问外网、不花钱
python3 smoke_test.py                           # 真实调用 Pollinations（需联网）
python3 smoke_test.py gemini                    # 测试某个已配好 key 的 provider
```

## 与规格不同或待确认的地方

代码里用 `TODO(规格待确认)` 标注，主要有：

- **参会者至少 2 位**，但同一个 provider 可以坐多个席位（换不同角色），所以只用 Pollinations 也能跑完整流程。
- **关掉“盲写首轮”**时，首轮改为依次公开发言，后发言者能看到前面的观点。
- 某一轮全部失败时停止后续轮次，但仍基于已有的成功发言做综合，状态记为“部分完成”。
- 综合报告的 max_tokens 至少给 2000，避免四个章节被截断。
- OpenRouter 和 xAI 的默认模型是占位值，刷新模型列表后会自动更新。
