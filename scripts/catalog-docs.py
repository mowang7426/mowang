#!/usr/bin/env python3
"""Generate a Chinese search/navigation index from validated catalog, never vice versa."""
import argparse
import collections
import html
from pathlib import Path
import catalog as c


def safe(value):
    value = value.encode('utf-8', 'backslashreplace').decode()
    return html.escape(value).replace('|', '&#124;').replace('`', '&#96;')


def generate(root):
    records, ledger = c.load_catalog(root)
    identities = collections.Counter(tuple(r['identity'][k] for k in ['package', 'version', 'architecture']) for r in records)
    lines = ['# 永久包管理记录（自动生成查找表）', '',
             '> main 的 catalog 发布模式启用后，删除对应 JSON 并提交即可下架单条记录；等待发布工作流成功。',
             '> 不要删除 processed ledger；旧 inbox 不会复活。空 catalog 全下架暂不支持。新上传中转包在可信发布成功后 future-only 精确自动清理；1439 条历史 path/blob 另需批准，R2 物理 GC 禁用。', '',
             '用 GitHub 页面内搜索包名、版本、架构，再点击记录链接。JSON 只存元数据；二进制永久留在 R2。',
             '本表由 catalog 生成，不是事实源；不要手改或删除 catalog-state/processed.json。', '',
             f'共 {len(records)} 条记录、{len({r["sha256"] for r in records})} 个内容对象。',
             f'同 Package/Version/Architecture 有多个记录的身份数：{sum(n > 1 for n in identities.values())}；全部保留，未擅自合并。', '',
             '| 包名 | 版本 | 架构 | SHA256 前16位 | 永久记录 |',
             '|---|---|---|---|---|']
    for r in sorted(records, key=lambda r: (r['identity']['package'], r['identity']['version'], r['identity']['architecture'], r['id'])):
        i = r['identity']
        lines.append(f'| {safe(i["package"])} | {safe(i["version"])} | {safe(i["architecture"])} | {r["sha256"][:16]} | [JSON]({r["id"]}.json) |')
    (Path(root) / 'packages' / 'README.md').write_text('\n'.join(lines) + '\n')
    print('Generated navigation for', len(records), 'records')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default='.')
    generate(p.parse_args().root)
