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
DEFAULT_PORT = 8000


def main() -> None:
    ap = argparse.ArgumentParser(description="AI 议事厅（AI Debate Sandbox）")
    ap.add_argument("--port", type=int, default=None, help="端口，默认 8000（被占用时自动往后找）")
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

    # 没指定 --port 时从 8000 起自动找可用端口：Windows 上 8000 常被占用或落在系统保留端口段（WinError 10013），
    # 在 WSL 里跑着的另一份实例也会占住 Windows 的 8000
    # 最后用 0 让系统分配一个随机可用端口：Hyper-V/WSL/Docker 常把 8000、8080 一带整段保留掉
    ports = [args.port] if args.port else list(range(DEFAULT_PORT, DEFAULT_PORT + 11)) + [0]
    httpd, errors, other_running = None, [], False
    for port in ports:
        if port and server.port_in_use("127.0.0.1", port):
            other_running = True
            errors.append(f"{port}：已被占用（可能是上次启动的 AI 议事厅还没关，包括在 WSL 里启动的那份）")
            continue
        try:
            httpd = server.make_server(host, port, extra)
            break
        except OSError as e:
            errors.append(f"{port or '系统分配的端口'}：无法监听（{e}；Windows 上可能是系统保留端口，"
                          "可用 netsh interface ipv4 show excludedportrange protocol=tcp 查看）")
    if not httpd:
        sys.exit("找不到可用端口：\n  " + "\n  ".join(errors) + "\n可以用 --port 指定其他端口，例如 python app.py --port 18000")
    port = httpd.server_address[1]
    if port != ports[0]:
        print(f"注意：端口 {errors[0]}，已改用 {port}", flush=True)
    if extra or host not in ("127.0.0.1", "localhost", "::1"):
        logging.info("监听 %s:%s，只接受本机%s的连接", host, port, f"和 Windows 主机 {extra[0]} " if extra else "")

    # 另一份实例可能还在跑（并共用 data/），这时不能把它进行中的辩论标记为中断
    n = 0 if other_running else store.mark_interrupted_on_startup()
    if n:
        logging.info("有 %d 场上次未完成的辩论，已标记为 interrupted", n)

    url = f"http://{HOST}:{port}"
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
