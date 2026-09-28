"""AI 议事厅入口：python3 app.py → 打开 http://127.0.0.1:8000

只用 Python 3.9+ 标准库，无需 pip install。
"""
import argparse
import logging
import sys
import threading
import webbrowser

if sys.version_info < (3, 9):
    sys.exit("需要 Python 3.9 或更高版本")

from backend import server, store  # noqa: E402

HOST = "127.0.0.1"  # 只监听本机


def main() -> None:
    ap = argparse.ArgumentParser(description="AI 议事厅（AI Debate Sandbox）")
    ap.add_argument("--port", type=int, default=8000, help="端口，默认 8000")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--debug", action="store_true", help="输出调试日志")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")

    busy = (f"端口 {args.port} 已被占用：可能是上次启动的 AI 议事厅还没关（先关掉那个窗口），"
            f"或者有其他程序在用这个端口。也可以换一个端口：python app.py --port 8080")
    if server.port_in_use(HOST, args.port):
        sys.exit(busy)
    try:
        httpd = server.make_server(HOST, args.port)
    except OSError as e:
        sys.exit(f"{busy}\n（{e}）")

    n = store.mark_interrupted_on_startup()
    if n:
        logging.info("有 %d 场上次未完成的辩论，已标记为 interrupted", n)

    url = f"http://{HOST}:{args.port}"
    print(f"AI 议事厅已启动：{url}  （Ctrl+C 退出）", flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
