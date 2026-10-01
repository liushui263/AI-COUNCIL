# AI 议事厅（AI Debate Sandbox）

[English](#english) · MIT License · 欢迎贡献，见 [CONTRIBUTING.md](CONTRIBUTING.md)

输入一个主题，让多个 AI 辩论几轮，最后输出一份标出**共识与分歧**的综合报告。
在你自己的电脑上本地运行，API Key 只存本机 `.env`。完整规格见 [docs/spec.md](docs/spec.md)。

## 运行

需要 Python 3.9 或更高版本，**不用 pip install 任何东西**。

```bash
git clone https://github.com/liushui263/AI-COUNCIL.git
cd AI-COUNCIL
python3 app.py          # Windows：python app.py
```

启动后会自动打开浏览器；也可以手动访问 <http://127.0.0.1:8000>。
默认端口 8000，被占用时自动改用 8001、8002…（启动窗口会显示实际地址）。可选参数：`--port 8080` 指定端口，`--no-browser` 不自动开浏览器，`--debug` 输出调试日志，`--host` 指定监听地址，`--shared` 共享模式（见“给朋友测试”）。

### 在 WSL 里运行

WSL2 把 Windows 的 `127.0.0.1` 转发进 WSL 时，对只监听 `127.0.0.1` 的程序不可靠，Windows 浏览器会报 `ERR_EMPTY_RESPONSE`。
所以在 WSL 里程序会自动改为监听 `0.0.0.0`，但只接受本机和 Windows 主机发来的连接，其他机器访问会被拒绝。
直接在 Windows 浏览器打开 <http://127.0.0.1:8000> 即可（WSL 里不会自动打开浏览器）。
如需强制只监听本机：`python3 app.py --host 127.0.0.1`。

## 第一次使用

1. 去“设置”页填几个免费 key（都不用绑卡），至少一个：
   - Gemini：<https://aistudio.google.com/app/apikey>
   - Groq：<https://console.groq.com/keys>
   - OpenRouter：<https://openrouter.ai/keys>（用 `:free` 结尾的模型）
   - Pollinations：<https://enter.pollinations.ai>（现在也需要 key 了）
2. 保存 key 时会自动获取该 provider 的模型列表，并从中挑一个当前可用的对话模型作为默认值（各家模型经常下线，所以不写死）。
   每次启动也会自动刷新超过一天的模型列表；需要时可在“设置”页手动点 **刷新模型列表**。
3. 在“新建辩论”页点 **一键勾选免费阵容**。只配了一家也能跑：同一家会以正反两个角色参会。模型名可以从下拉框选，也可以手动输入。

## 免费档的限流

免费档经常返回 429（限流，Groq 按分钟算 token）或 503（服务繁忙，Gemini 高峰期常见），程序会这样处理：

- 按服务端提示的时间等待后重试（例如 “try again in 32s” 就等 32 秒），没提示就等 2、8 秒。每位参会者每轮默认最多重试 2 次（可在“高级参数”或右侧“参会者与设置”里改成 0~5 次），还失败就跳过，交给下一位参会者。等待期间发言位置会显示实时倒计时；要等一分钟以上（如每日额度用完）就直接判为失败。
- 同一个 provider 的多个席位依次调用，不同 provider 之间并行，避免自己抢自己的额度。
- 交叉轮引用他人发言时，每段最多 3000 字，控制 token。
- 综合者失败时，自动换参会者里的其他模型来写报告，报告上会注明实际由谁综合。
- **推理模型的截断**：gpt-oss、Qwen3、Gemini 2.5 等会先“思考”，思考也算在输出上限里。所以默认最大输出长度是 4096 tokens（高级参数里可调到 16384），
  能控制思考强度的模型会自动设为“低”（Groq 的 `reasoning_effort`、OpenRouter 的 `reasoning.effort`、Gemini 的思考预算 512），提示词也要求 800 字以内。
  仍被截断时自动把上限加倍（至少 4096）重新生成一次；如果思考把额度用光、一个字正文都没有，就判为失败并提示换模型或调大长度。

## 上传文档

新建辩论和续辩的输入框都有 **📎 上传文档**（也可以把文件拖进输入框），支持 txt、md、pdf、docx，一次最多 5 个、每个不超过 10MB。
文字在本机提取（不装任何依赖），和背景材料/补充内容一起发给每位参会者；每个文件最多发送前 2 万字，免费档按 token 限流，文件太长容易 429。
PDF 只能提取文字层：扫描件（图片）和加密 PDF 会提示无法读取；旧版 .doc 请先另存为 .docx。综合报告只列出附件文件名，不再重复全文。

## 导出

综合报告下方和“参会者与设置”里有三个导出按钮：

- **导出 .md**：Markdown 原文。
- **Word**：直接下载 .docx，可以用 Word、WPS 打开编辑。
- **PDF**：打开一个排好版的打印页，并自动弹出打印窗口，在“目标打印机”里选 **另存为 PDF**。
  （生成 PDF 需要排版和中文字体，纯标准库做不好，所以交给浏览器。）

## 界面与接着辩

界面分三栏：左边是历史，中间是辩论对话，右边是综合报告和参会者设置（窄屏时左右两栏收成抽屉）。右栏可以点 **隐藏侧栏** 收起，再点 **显示侧栏** 打开。

参会者每人一行：勾选、模型图标、角色名、模型下拉。彩色的角色名本身就是下拉菜单，点它换角色；每种角色有自己的颜色和字体（中立蓝、支持绿、反对红……）。预设有 中立 / 支持 / 反对 / 财务 / 开发 / 风险 / 体验 / 执行 / 挑刺，也可以选“自定义…”自己写。点 ＋ 加人时自动选第一个还没人用的角色。续辩时“添加参会者”下拉里列出所有 provider 的模型，选了没配 key 的会在那一行给出配置链接。图标是内置的简化版，不从外网加载。

综合报告出来之后，可以在中间底部的输入框里**补充新的内容或问题**，选“再辩几轮”，点 **继续辩论**：
每位参会者都会看到你的补充（以及上一份综合报告），辩完再生成一份新的综合报告，旧报告在右侧可以切换查看。
总轮数不设上限（单次最多追加 10 轮）。续辩前可以在右侧“参会者与设置”里**换模型、改角色、换综合者**，对之后的发言生效；点每行末尾的 **＋** 或下方“添加参会者”可以**中途加人**（从下一轮开始发言，最多 8 位）；
有失败的发言时会先重试它们，也可以单独点 **重试失败的发言** 或 **重新综合**。

付费 provider（DeepSeek、OpenAI、Anthropic、xAI）费用走你自己的账户，选中时页面会有黄色提示。
注意：Claude Pro / Claude Code 订阅不含 API 额度，Anthropic 需要单独开 API 账单。

## 给朋友测试（共享模式）

默认是单机模式，只有你自己在本机用。想让朋友也来用，就用共享模式启动：

- 每个人进来要输入**访问密码**（你告诉朋友），再用自己的名字和密码登录；名字第一次用就自动注册。
- **每人的历史记录分开**，页面上谁也看不到别人的辩论；**设置页也是各人自己的**。
- **朋友用自己的 key**：key 只保存在他们自己的浏览器里，发起辩论时随请求发到你的电脑，只在辩论进行期间留在内存里，不写硬盘、不记日志。
  你 `.env` 里的 key 在共享模式下**不会给任何人用**（你自己在共享模式里也要在设置页填一次）。
- 同一 IP 10 分钟内登录失败 10 次会被暂时锁住；登录状态保存在内存里，重启程序后需要重新登录。
- 注意：辩论存档保存在你电脑的 `data/debates/` 里（带 owner 字段），你作为主机能在硬盘上看到这些文件，页面上看不到。

### 1. 设访问密码并启动

在 `.env` 里加一行（至少 8 位）：

```
AI_COUNCIL_ACCESS_PASSWORD=换成你自己的密码
```

然后：

```bash
python app.py --shared --port 8000
```

### 2. 让朋友连进来（二选一）

**Tailscale（推荐，只有你邀请的人能连）**

1. 你和朋友都装 Tailscale（<https://tailscale.com/download>）并登录；在 Tailscale 管理页把你这台电脑 **Share** 给朋友（或邀请他们加入你的 tailnet）。
2. 在你电脑上运行 `tailscale serve --bg 8000`，它会给出一个 `https://你的电脑名.xxx.ts.net` 地址（只有 tailnet 里的人能访问，自带 HTTPS）。
3. 把这个地址和访问密码发给朋友。

不想用 `tailscale serve` 的话，也可以 `python app.py --shared --host 0.0.0.0`，朋友访问 `http://你的Tailscale IP:8000`（流量由 Tailscale 加密；Windows 防火墙弹窗时允许专用网络）。

**Cloudflare Tunnel（不用朋友装任何东西）**

1. 安装 cloudflared（<https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/>）。
2. 运行 `cloudflared tunnel --url http://127.0.0.1:8000`，它会给出一个 `https://随机名字.trycloudflare.com` 地址。
3. 把地址和访问密码发给朋友。这个地址是公开的，任何拿到链接的人都能看到登录页，所以访问密码要设得够长；
   另外 HTTPS 在 Cloudflare 那一端解密，朋友的 key 会经过 Cloudflare。每次重启 cloudflared 地址会变。

测试完关掉 cloudflared 或运行 `tailscale serve reset`，再用 `python app.py`（不带 `--shared`）就回到单机模式。

## 安全

- key 只保存在本目录的 `.env` 文件里；页面和所有接口只显示“已配置/未配置”，日志里不记录 key。
- **不要把 `.env` 发给任何人**。`.env` 和 `data/` 已在 `.gitignore` 里。
- 单机模式下服务只监听 `127.0.0.1`（WSL 里见上文），只接受本机连接，并拒绝来自其他网站的跨站请求；共享模式见上文，需要登录才能用。
- 本应用除了你选择的 provider 之外，不向任何地址发请求。

## 目录

```
AI-COUNCIL/
├── app.py                 # 入口
├── backend/
│   ├── server.py          # HTTP 路由 + SSE
│   ├── debate.py          # 辩论引擎：轮次编排、并发、提示词、综合
│   ├── providers.py       # 8 家 provider 适配器（统一 chat 接口）
│   ├── extract.py         # 上传文档提取文字（txt/md/docx/pdf，纯标准库）
│   ├── export.py          # 导出 Word（.docx）和打印页（另存为 PDF）
│   ├── auth.py            # 共享模式的登录（访问密码 + 个人账号）
│   └── store.py           # .env、JSON 存档、模型列表缓存
├── frontend/index.html    # 单文件前端（无框架、无 CDN）
├── data/                  # 辩论存档与模型缓存（自动生成）
├── smoke_test.py          # 真实调用一次已配置 key 的 provider，验证网络链路
└── tests/                 # 离线端到端测试（用本地假 provider）
```

## 测试

```bash
python3 -m unittest discover -s tests -t . -v   # 离线测试，不访问外网、不花钱
python3 smoke_test.py gemini                    # 真实调用某个已配好 key 的 provider（需联网）
```

## 与规格不同或待确认的地方

代码里用 `TODO(规格待确认)` 标注，主要有：

- **参会者至少 2 位**，但同一个 provider 可以坐多个席位（换不同角色），所以只配一家免费 key 也能跑完整流程。
- **关掉“盲写首轮”**时，首轮改为依次公开发言，后发言者能看到前面的观点。
- 某一轮全部失败时停止后续轮次，但仍基于已有的成功发言做综合，状态记为“部分完成”。
- 默认最大输出长度 4096（规格为 1500），综合报告至少 4096，避免推理模型和四个章节被截断。
- OpenRouter 和 xAI 的默认模型是占位值，刷新模型列表后会自动更新。
- 429/503 的重试会按服务端提示等待（规格是 1s、4s 两次），见“免费档的限流”；综合者失败时会自动换人。

## English

**AI Council** lets several AI models debate a topic for a few rounds, then writes a combined report that marks where they agree and where they disagree.
It runs on your own computer with Python 3.9+ and the standard library only (no `pip install`). API keys stay in a local `.env` file.

```bash
git clone https://github.com/liushui263/AI-COUNCIL.git
cd AI-COUNCIL
python3 app.py            # Windows: python app.py
```

It opens <http://127.0.0.1:8000>. Add at least one key on the Settings page (Gemini, Groq, OpenRouter and Pollinations have free tiers), then start a debate.
Supported providers: Gemini, Groq, OpenRouter, Pollinations, DeepSeek, OpenAI, Anthropic and xAI. Debates can be exported as Markdown, Word or PDF.

Run the offline tests with `python3 -m unittest discover -s tests -t .`. See [CONTRIBUTING.md](CONTRIBUTING.md) to contribute, and [LICENSE](LICENSE) for the MIT license.
