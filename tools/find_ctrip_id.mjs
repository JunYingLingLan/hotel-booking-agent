// 用搜索引擎定位携程酒店 ID（Node 的 TLS 指纹能拿到完整结果页）
// 用法: node find_ctrip_id.mjs "沈阳雅高瑞士酒店" "沈阳"
// 输出: {"id": "371184"} 或 {"id": ""}
import { argv } from 'node:process';

const name = argv[2] || '';
const city = argv[3] || '';

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

const engines = [
  // 360 最稳；百度会偶发限流（返回 1.4KB 精简页）；搜狗经常直接反爬
  { url: 'https://www.so.com/s', param: 'q' },
  { url: 'https://www.baidu.com/s', param: 'wd' },
  { url: 'https://www.sogou.com/web', param: 'query' },
];

const query = [name, city, '携程'].filter(Boolean).join(' ');

function coreName(s) {
  return (s || '').replace(/[（(].*?[)）]/g, '').replace(/\s+/g, '');
}

async function tryEngine(engine) {
  const u = `${engine.url}?${engine.param}=${encodeURIComponent(query)}`;
  try {
    const r = await fetch(u, {
      headers: {
        'User-Agent': UA,
        'Accept-Language': 'zh-CN,zh;q=0.9',
        Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
      },
      redirect: 'follow',
      signal: AbortSignal.timeout(15000),
    });
    if (!r.ok) return null;
    const html = await r.text();
    const ids = [
      ...html.matchAll(/hotels\.ctrip\.com\/(?:hotels\/)?(\d{5,12})\.html/g),
    ].map((m) => m[1]);
    return [...new Set(ids)];
  } catch {
    return null;
  }
}

const target = coreName(name);
for (const engine of engines) {
  const ids = await tryEngine(engine);
  if (!ids || !ids.length) continue;
  for (const id of ids.slice(0, 3)) {
    // 用详情页标题校验是不是这家酒店
    try {
      const r = await fetch(`https://hotels.ctrip.com/hotels/${id}.html`, {
        headers: {
          'User-Agent': UA,
          'Accept-Language': 'zh-CN,zh;q=0.9',
          Referer: 'https://hotels.ctrip.com/',
        },
        signal: AbortSignal.timeout(15000),
      });
      const html = await r.text();
      const title = (html.match(/<title>(.*?)<\/title>/) || ['', ''])[1];
      if (target && coreName(title).includes(target)) {
        console.log(JSON.stringify({ id, title: title.slice(0, 40) }));
        process.exit(0);
      }
    } catch {
      /* 换下一个候选 */
    }
  }
}
console.log(JSON.stringify({ id: '' }));
