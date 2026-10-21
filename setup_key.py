# -*- coding: utf-8 -*-
"""
一键配置道旅 RollingGo Key。

做三件事：
  1.（可选）打开注册页，你注册并复制 mcp_ 开头的 Key
  2. 校验 Key 是否真的能用（向 MCP 发一次 initialize，看返回码）
  3. 写进 .env 的 ROLLINGGO_API_KEY，下次启动自动生效

用法：
  python setup_key.py                # 交互式：提示粘贴 Key
  python setup_key.py --open         # 先打开注册页，再提示粘贴
  python setup_key.py --check mcp_x  # 只校验，不写入
  python setup_key.py --key mcp_x    # 非交互写入

安全：Key 只写进本地 .env（.gitignore 已排除），不发送到除道旅以外的任何地方。
"""
import argparse
import os
import re
import sys
import webbrowser

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, '.env')
EXAMPLE_PATH = os.path.join(HERE, '.env.example')
MCP_URL = 'https://mcp.rollinggo.cn/mcp'
REGISTER_URL = 'https://travelportal-partner-center.dida.com/register?lang=zh'
KEY_RE = re.compile(r'^mcp_[A-Za-z0-9_\-]{8,}$')


def verify_key(key: str):
    """向 MCP 发一次 initialize，用状态码判断 Key 是否有效。"""
    payload = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {'name': 'setup_key', 'version': '1.0'},
        },
    }
    headers = {
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
    }
    try:
        r = requests.post(MCP_URL, json=payload, headers=headers, timeout=20)
    except requests.RequestException as e:
        return None, f'网络异常：{e}'
    if r.status_code in (200, 202):
        return True, f'HTTP {r.status_code}，Key 有效'
    if r.status_code in (401, 403):
        return False, f'HTTP {r.status_code}，Key 无效或已失效'
    return None, f'HTTP {r.status_code}：{r.text[:160]}'


def write_env(key: str) -> str:
    """写入 .env；保留文件里其他配置。"""
    if not os.path.exists(ENV_PATH):
        if os.path.exists(EXAMPLE_PATH):
            with open(EXAMPLE_PATH, encoding='utf-8') as f:
                base = f.read()
        else:
            base = 'ROLLINGGO_API_KEY=\n'
        with open(ENV_PATH, 'w', encoding='utf-8', newline='\n') as f:
            f.write(base)

    with open(ENV_PATH, encoding='utf-8') as f:
        lines = f.readlines()

    line = f'ROLLINGGO_API_KEY={key}\n'
    replaced = False
    for i, ln in enumerate(lines):
        if re.match(r'^\s*ROLLINGGO_API_KEY\s*=', ln):
            lines[i] = line
            replaced = True
            break

    if not replaced:
        lines.append('\n' + line)

    with open(ENV_PATH, 'w', encoding='utf-8', newline='\n') as f:
        f.writelines(lines)
    return ENV_PATH


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--key', default='')
    ap.add_argument('--check', default='')
    ap.add_argument('--open', action='store_true')
    a = ap.parse_args()

    if a.check:
        ok, msg = verify_key(a.check.strip())
        print(msg)
        sys.exit(0 if ok else 1)

    if a.open or not a.key:
        print('正在打开道旅注册页…')
        try:
            webbrowser.open(REGISTER_URL)
        except Exception:
            pass
        print(f'如果浏览器没自动打开，手动访问：{REGISTER_URL}')
        print()

    key = a.key.strip()
    if not key:
        print('注册后在控制台复制以 mcp_ 开头的 Key，粘贴到这里（回车确认）：')
        key = input('Key > ').strip()

    if not key:
        print('没有输入内容，已取消。')
        sys.exit(1)

    if not KEY_RE.match(key):
        print(f'⚠ 格式看起来不对：{key[:8]}…（应以 mcp_ 开头，长度 ≥ 12）')
        print('  仍然继续校验，请确认没有多复制空格或引号。')

    print('正在向道旅校验 Key…')
    ok, msg = verify_key(key)
    print('  ', msg)

    if ok is False:
        print('校验未通过，没有写入 .env。请确认 Key 是否正确、是否已过期。')
        sys.exit(1)
    if ok is None:
        print('无法确认（可能是网络或服务波动）。仍可写入，但启动时可能回退模拟数据。')
        if input('仍然写入吗？[y/N] ').strip().lower() != 'y':
            sys.exit(1)

    path = write_env(key)
    print()
    print('✅ 已写入：', path)
    print('   重启程序（桌面「酒店Agent-Web版.cmd」/「酒店Agent-CLI版.cmd」）即生效。')
    print('   下单仍需 OAuth 登录：python rollinggo_book.py login')


if __name__ == '__main__':
    main()
