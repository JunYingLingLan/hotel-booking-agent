# -*- coding: utf-8 -*-
"""
携程房型图集（只取图片，不碰价格与库存）。

背景
----
- 道旅 MCP 每个房型只返回一张官方图；
- 携程酒店详情页可匿名访问，页面内嵌的 subImgUrlList JSON 里
  imgTitle 就是房型名，每个房型 1~9 张图，房型命名与道旅高度一致；
- 携程站内搜索不响应关键词、浏览器访问会跳登录页，
  但百度/搜狗已索引 hotels.ctrip.com/hotels/{id}.html，可据此定位 hotelId。

用途声明
--------
仅用于个人学习与教学演示，作为酒店预订教学 Agent 的图片补充组件。
只读取酒店详情页里**公开的图片标题与直链**，不抓取价格、库存、会员信息；
不批量遍历、不用于商业用途。酒店图片版权归原平台及酒店所有。

调用入口
--------
    from ctrip_gallery import fetch_gallery_for
    gallery = fetch_gallery_for("沈阳雅高瑞士酒店", "沈阳")   # {房型名: [图url]}
    urls = match_room("豪华大床房（舒适空间+手机投屏）", gallery)
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional

import requests

from models import Hotel

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
HDR = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': 'https://hotels.ctrip.com/',
}
_TIMEOUT = (6, 15)
_CACHE_TTL = 7 * 24 * 3600
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
_CACHE_PATH = os.path.join(_CACHE_DIR, 'ctrip_gallery.json')

_lock = threading.Lock()
_disk: Optional[dict] = None


def _load_disk() -> dict:
    global _disk
    with _lock:
        if _disk is not None:
            return _disk
        try:
            with open(_CACHE_PATH, encoding='utf-8') as f:
                data = json.load(f)
                _disk = data.get('items', {})
        except (OSError, ValueError):
            _disk = {}
        return _disk


def _save_disk() -> None:
    with _lock:
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            with open(_CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump({'version': 1, 'items': _disk or {}}, f,
                          ensure_ascii=False)
        except OSError:
            pass


def _core_name(name: str) -> str:
    """去掉括号补充说明，取主名用于比对。"""
    s = re.sub(r'[（(].*?[)）]', '', name or '')
    return re.sub(r'\s+', '', s)


def _fetch_title_and_gallery(hotel_id: str):
    """抓详情页，返回 (title, {分组名: [图片直链]})。"""
    url = f'https://hotels.ctrip.com/hotels/{hotel_id}.html'
    r = requests.get(url, headers=HDR, timeout=_TIMEOUT)
    if r.status_code != 200:
        return '', {}
    html = r.text
    title = (re.search(r'<title>(.*?)</title>', html) or ['', ''])[1]

    raw = html.replace('\\"', '"')
    block = re.search(r'"subImgUrlList"\s*:\s*(\[.*?\])\s*[,}]', raw, flags=re.S)
    items = []
    if block:
        try:
            items = json.loads(block.group(1))
        except json.JSONDecodeError:
            items = []
    groups: Dict[str, List[str]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        link = str(it.get('link') or '').strip()
        if not link:
            continue
        if link.startswith('//'):
            link = 'https:' + link
        if not link.startswith('https://dimg'):
            continue
        key = str(it.get('imgTitle') or '').strip() or '未分类'
        groups.setdefault(key, []).append(link)
    return title, groups


def find_hotel_id(name: str, city: str = '') -> Optional[str]:
    """
    用搜索引擎定位携程酒店 ID。

    必须走 Node 子进程：Python requests 的 TLS 指纹会被搜索引擎识别成爬虫
    （百度只返回 1.4KB 精简页、搜狗直接反爬），而 Node 的 fetch 能拿到完整结果页。
    """
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'tools', 'find_ctrip_id.mjs')
    if os.path.exists(script):
        node = shutil.which('node') or 'node'
        try:
            out = subprocess.run([node, script, name, city],
                                 capture_output=True, text=True, timeout=45,
                                 encoding='utf-8', errors='replace')
            payload = (out.stdout or '').strip().splitlines()
            if payload:
                data = json.loads(payload[-1])
                if data.get('id'):
                    return str(data['id'])
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    # 兜底：Python 直连（命中率低，仅在 Node 不可用时尝试）
    query = f'{name} {city} 携程'.strip()
    for base, params in (
            ('https://www.so.com/s', {'q': query}),
            ('https://www.baidu.com/s', {'wd': query})):
        try:
            r = requests.get(base, params=params, headers=HDR, timeout=_TIMEOUT)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        ids = re.findall(r'hotels\.ctrip\.com/(?:hotels/)?(\d{5,12})\.html', r.text)
        for hid in list(dict.fromkeys(ids))[:3]:
            try:
                title, _ = _fetch_title_and_gallery(hid)
            except requests.RequestException:
                continue
            if _core_name(name) and _core_name(name) in _core_name(title):
                return hid
    return None


def fetch_gallery_for(name: str, city: str = '') -> Dict[str, List[str]]:
    """按酒店名取携程图集（带 7 天磁盘缓存）。失败返回空 dict。"""
    if not name:
        return {}
    key = f'{city}|{name}'
    disk = _load_disk()
    hit = disk.get(key)
    if hit and time.time() - hit.get('ts', 0) < _CACHE_TTL:
        return hit.get('rooms', {})

    try:
        hid = find_hotel_id(name, city)
        if not hid:
            return {}
        title, groups = _fetch_title_and_gallery(hid)
        if not groups:
            return {}
        rooms = {k: v for k, v in groups.items() if re.search(r'房|床|套', k)}
        disk[key] = {'ts': time.time(), 'hotel_id': hid, 'rooms': rooms}
        _save_disk()
        return rooms
    except Exception:
        # 网络/解析异常绝不阻塞订房主流程
        return {}


def match_room(room_name: str, gallery: Dict[str, List[str]]) -> List[str]:
    """把道旅房型名匹配到携程图集，返回该房型的图片列表。"""
    if not room_name or not gallery:
        return []
    norm = lambda s: re.sub(r'[\s（）()【】\[\]丨|+，,]', '', s or '')
    target = norm(room_name)
    if not target:
        return []

    # 1) 完全一致
    for title, urls in gallery.items():
        if norm(title) == target:
            return urls

    # 2) 携程标题包含道旅名（携程名通常更长）
    for title, urls in gallery.items():
        if target in norm(title):
            return urls

    # 3) 道旅名包含携程名
    for title, urls in gallery.items():
        t = norm(title)
        if len(t) >= 4 and t in target:
            return urls

    # 4) 前缀相同（"豪华大床房" vs "豪华大床房丨..."）
    for title, urls in gallery.items():
        t = norm(title)
        if len(t) >= 4 and (t.startswith(target[:6]) or target.startswith(t[:6])):
            return urls
    return []


def enrich_hotel_room_images(hotel: Hotel, max_per_room: int = 3) -> int:
    """给酒店的每个房型补充多张实拍图，返回补充成功的房型数。"""
    if not hotel.rate_plans:
        return 0
    gallery = fetch_gallery_for(hotel.name, hotel.city)
    if not gallery:
        return 0
    filled = 0
    for rp in hotel.rate_plans:
        urls = match_room(rp.room_name, gallery)
        if urls:
            rp.image_urls = urls[:max_per_room]
            if not rp.image_url:
                rp.image_url = urls[0]
            filled += 1
        elif rp.image_url and not rp.image_urls:
            rp.image_urls = [rp.image_url]
    return filled
