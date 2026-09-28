"""链路冒烟测试：真实调用一次已配置 key 的 provider（需要能访问外网）。

运行：python3 smoke_test.py gemini            （Windows 上用 python）
可选：python3 smoke_test.py groq 模型名        （指定模型）
不带参数时用第一个已配置 key 的 provider。
"""
import sys
import time

from backend import providers


def main() -> int:
    configured = [p["id"] for p in providers.PROVIDERS if providers.key_configured(p["id"])]
    pid = sys.argv[1] if len(sys.argv) > 1 else (configured[0] if configured else "")
    if not pid:
        print("还没有配置任何 key：先运行 app.py 在设置页填一个，或写进 .env")
        return 1
    model = sys.argv[2] if len(sys.argv) > 2 else providers.default_model(pid)
    print(f"测试 {pid} / {model}：", flush=True)
    t = time.time()
    try:
        res = providers.chat(pid, model, "你是一位简洁的助手。", "用一句话说明你是谁。",
                             max_tokens=200, temperature=0.7, stream=True,
                             on_token=lambda d: print(d, end="", flush=True))
    except providers.ProviderError as e:
        print(f"\n失败：{e}")
        return 1
    print(f"\n\n成功，用时 {time.time() - t:.1f}s，usage={res.get('usage')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
