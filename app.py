"""AI 议事厅入口：python3 app.py → 打开 http://127.0.0.1:8000

只用 Python 3.9+ 标准库，无需 pip install。
"""
import argparse
import logging
import os
import sys
import threading
import webbrowser

if sys.version_info < (3, 9):
    sys.exit("需要 Python 3.9 或更高版本")

from backend import server, store  # noqa: E402

HOST = "127.0.0.1"  # 默认只监听本机


def main() -> None:
    ap = argparse.ArgumentParser(description="AI 议事厅（AI Debate Sandbox）")
    ap.add_argument("--port", type=int, default=8000, help="端口，默认 8000")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--debug", action="store_true", help="输出调试日志")
    ap.add_argument("--host", default=os.environ.get("AI_COUNCIL_HOST"),
                    help="监听地址，默认 127.0.0.1；在 WSL 里默认 0.0.0.0（仍只接受 Windows 本机的连接）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")

    # WSL2 的 localhost 转发对只监听 127.0.0.1 的程序不可靠，Windows 浏览器会收到 ERR_EMPTY_RESPONSE。
    # 所以在 WSL 里默认监听 0.0.0.0，但只放行本机和 Windows 主机（默认网关）发来的连接。
    wsl = server.is_wsl()
    host = args.host or ("0.0.0.0" if wsl else HOST)
    extra = []
    if host not in ("127.0.0.1", "localhost", "::1"):
        gw = server.wsl_windows_host_ip() if wsl else None
        if gw:
            extra.append(gw)
        logging.info("监听 %s:%s，只接受本机%s的连接", host, args.port, f"和 Windows 主机 {gw} " if gw else "")

    busy = (f"端口 {args.port} 已被占用：可能是上次启动的 AI 议事厅还没关（先关掉那个窗口），"
            f"或者有其他程序在用这个端口。也可以换一个端口：python app.py --port 8080")
    if server.port_in_use("127.0.0.1", args.port):
        sys.exit(busy)
    try:
        httpd = server.make_server(host, args.port, extra)
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
