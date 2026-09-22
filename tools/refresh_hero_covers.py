#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给已有封面补 / 重算超分缓存（``static/cover/hero/``）。

背景：源站封面普遍只有 320px 宽，主页 hero 显示宽度 1267px（1920 视口）→
浏览器把 320px 拉 4 倍，糊得肉眼可见。入库时三个 provider 与「上传封面」
都会自动生成超分缓存，但**功能上线前入库的封面没有缓存**，需要本工具补一次。

用法（在 MangaDock 仓库根目录执行）::

    # 体检：列出哪些封面缺缓存 / 缓存过期（不写任何文件）
    python tools/refresh_hero_covers.py

    # 只补缺失的（已存在的跳过）
    python tools/refresh_hero_covers.py --apply

    # 全部重算（换引擎 / 调过参数后用）
    python tools/refresh_hero_covers.py --apply --force

    # 只看会用到哪个超分引擎
    python tools/refresh_hero_covers.py --engine

退出码：0 = 无需处理或已处理完；1 = 仅体检且发现缺缓存。
"""
import argparse
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mangadock.settings import COVER_ROOT  # noqa: E402
from mangadock.utils.cover_enhance import (  # noqa: E402
    HERO_TARGET_EDGE,
    backfill_hero_covers,
    hero_cover_path,
    hero_cover_ready,
    source_cover_path,
    sr_engine_name,
)


def list_covers():
    if not os.path.isdir(COVER_ROOT):
        return []
    names = []
    for entry in sorted(os.listdir(COVER_ROOT)):
        if entry.startswith('.') or not entry.lower().endswith(('.jpg', '.jpeg', '.png')):
            continue
        if entry.lower() == 'cover.png':      # 占位图，不需要超分
            continue
        names.append(os.path.splitext(entry)[0])
    return names


def main():
    parser = argparse.ArgumentParser(description='封面超分缓存体检 / 补算')
    parser.add_argument('--apply', action='store_true', help='实际生成缓存（默认只体检）')
    parser.add_argument('--force', action='store_true', help='已存在的缓存也重算')
    parser.add_argument('--engine', action='store_true', help='只打印当前使用的超分引擎')
    args = parser.parse_args()

    if args.engine:
        print(f'当前超分引擎: {sr_engine_name()}（目标长边 {HERO_TARGET_EDGE}）')
        return 0

    names = list_covers()
    if not names:
        print(f'封面目录为空: {COVER_ROOT}')
        return 0

    missing = [n for n in names if not hero_cover_ready(n)]
    print(f'封面目录: {COVER_ROOT}')
    print(f'封面总数: {len(names)}   已有新鲜缓存: {len(names) - len(missing)}   待处理: {len(missing)}')
    print(f'超分引擎: {sr_engine_name()}   目标长边: {HERO_TARGET_EDGE}')

    if not args.apply:
        if missing:
            print('\n缺缓存 / 缓存过期的封面:')
            for name in missing:
                source = source_cover_path(name)
                size = os.path.getsize(source) // 1024 if source and os.path.isfile(source) else -1
                print(f'  · {name}  ({size} KB)  → {hero_cover_path(name)}')
            print('\n加上 --apply 生成。')
            return 1
        print('全部封面都有新鲜缓存，无需处理。')
        return 0

    started = time.time()

    def progress(index, total, name):
        print(f'  [{index}/{total}] {name}', flush=True)

    done, skipped, failed = backfill_hero_covers(force=args.force, on_progress=progress)
    elapsed = time.time() - started
    print(f'\n完成：新生成 {done}，跳过 {skipped}，失败 {failed}，用时 {elapsed:.1f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
