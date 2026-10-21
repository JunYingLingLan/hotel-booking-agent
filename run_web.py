# -*- coding: utf-8 -*-
"""
酒店预订 Agent · Web 启动器

特点：
  - 自动挑选空闲端口（从 8010 起找，被占用就 +1），不会和别的程序抢端口
  - 启动后把真实地址打印在窗口里，并自动打开浏览器
  - 加 --no-browser 只启动服务、不开浏览器
  - 加 --port 8123 指定端口

用法：
  python run_web.py
  python run_web.py --no-browser
  python run_web.py --port 9000
"""
import os
import socket
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)


def pick_port(start: int = 8010, tries: int = 50) -> int:
    """从 start 起找第一个能绑上的端口；全被占则交给系统分配。"""
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', p))
                return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def open_later(url: str, delay: float = 2.0) -> None:
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def main():
    args = sys.argv[1:]
    no_browser = '--no-browser' in args
    port = None
    if '--port' in args:
        try:
            port = int(args[args.index('--port') + 1])
        except (IndexError, ValueError):
            port = None
    if port is None:
        port = pick_port()

    url = f'http://127.0.0.1:{port}/'
    bar = '=' * 56
    print(bar)
    print('  酒店预订 Agent  ·  Web 版')
    print(f'  地址：{url}')
    print('  提示：关闭这个窗口即停止服务')
    print(bar)
    print(flush=True)

    if not no_browser:
        open_later(url)

    import uvicorn
    uvicorn.run('web:app', host='127.0.0.1', port=port, log_level='warning')


if __name__ == '__main__':
    main()
