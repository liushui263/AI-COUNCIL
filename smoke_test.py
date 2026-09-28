"""链路冒烟测试：用免 key 的 Pollinations 真实调用一次（需要能访问外网）。

运行：python3 smoke_test.py            （Windows 上用 python smoke_test.py）
可选：python3 smoke_test.py gemini     （测试某个已在 .env 配好 key 的 provider）
"""
import sys
import time

from backend import providers


def main() -> int:
    pid = sys.argv[1] if len(sys.argv) > 1 else "pollinations"
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
