# 参与贡献 / Contributing

欢迎提 Issue 和 Pull Request。

## 约定

- **只用 Python 标准库**（3.9+），不引入任何 pip 依赖；前端保持单文件 `frontend/index.html`，不用框架和 CDN。
- 提交前跑一遍离线测试，必须全部通过：
  ```bash
  python3 -m unittest discover -s tests -t .
  ```
  新功能请顺带加测试（`tests/mock_providers.py` 里有本地假 provider，不花钱、不联网）。
- **绝不提交 API key**。`.env` 和 `data/` 已在 `.gitignore` 里；测试里只用假 key。
- 一个 PR 只做一件事，说明改了什么、为什么，界面改动附截图。

## 改动如何回流

这个仓库是 AI 议事厅的开源版本，维护者的其他项目会定期从这里同步：
合并进 `main` 的改动会被引入维护者的主项目；其中好的功能也可能移植到网页版（纯浏览器版本，不在本仓库）。
所以请尽量保持模块边界清晰（provider 适配在 `backend/providers.py`，辩论编排在 `backend/debate.py`），方便移植。

提交贡献即表示你同意按 [MIT 许可证](LICENSE) 发布你的代码。

---

Issues and pull requests are welcome. Keep to the Python standard library (no pip dependencies) and a single-file frontend,
run `python3 -m unittest discover -s tests -t .` before submitting, never commit API keys, and keep each PR to one change.
Changes merged into `main` are pulled into the maintainer's main project, and good features may be ported to the web version.
By contributing you agree your code is released under the [MIT License](LICENSE).
