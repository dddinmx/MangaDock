#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""封面图片体检与修复工具。

扫描封面目录，找出两类问题文件并（可选）就地修复：

1. **无法解码**：文件根本不是图片，或格式本机不支持；
2. **格式错配**：能被 Pillow 打开，但真实格式与扩展名不一致（典型是
   WebP/AVIF/PNG 内容顶着 ``.jpg`` 名字）。静态服务按扩展名下发
   ``Content-Type``，Safari / WebKit 据此挑解码器，遇到不匹配会解码失败，
   前端回落到默认封面；Chrome / Firefox 因为会嗅探内容所以看不出来，
   这类问题因此容易长期潜伏。

修复时按扩展名写回正确格式（``.jpg`` → JPEG，``.png`` → PNG），
保持文件名与内容一致，避免修完又变成另一种错配。

用法（在 MangaDock 仓库根目录执行）::

    # 只体检，不改动任何文件
    python tools/repair_cover_images.py

    # 确认问题后修复（原文件先备份）
    python tools/repair_cover_images.py --apply

    # 指定目录与备份位置
    python tools/repair_cover_images.py --apply --root static/cover --backup-dir /tmp/bk

退出码：0 = 无问题或已修复；1 = 仅体检且发现问题（便于脚本判断）。
"""
import argparse
import os
import shutil
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from PIL import Image  # noqa: E402

from mangadock.utils.cover_image import (  # noqa: E402
    OUTPUT_FORMATS,
    expected_format_for_path,
    is_readable_image,
    normalize_cover_file,
    sniff_image_format,
)

DEFAULT_ROOTS = (
    'static/cover',
    'static/cover/hero',
    'static/cover/novels',
    'static/login-covers',
)
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.avif', '.heic', '.gif', '.bmp', '.tiff')


def describe(path):
    """返回 (是否正常, 问题描述)。"""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, f'无法读取: {exc}'

    with open(path, 'rb') as handle:
        head = handle.read(64)

    detected = sniff_image_format(head)

    try:
        with Image.open(path) as image:
            actual = image.format
            image.load()
    except Exception as exc:
        return False, f'解码失败（文件头判定 {detected or "未知"}, {size}B, {type(exc).__name__}）'

    suffix = os.path.splitext(path)[1].lower()
    if suffix in OUTPUT_FORMATS and actual != OUTPUT_FORMATS[suffix]:
        return False, f'格式错配（扩展名 {suffix} 实际为 {actual}, {size}B）'

    return True, f'{actual} {size}B'


def collect_files(roots, repo_root):
    targets = []
    for rel in roots:
        directory = rel if os.path.isabs(rel) else os.path.join(repo_root, rel)
        if not os.path.isdir(directory):
            continue
        for current, _dirs, files in os.walk(directory):
            for name in sorted(files):
                if name.startswith('._') or name == '.DS_Store':
                    continue
                if name.lower().endswith(IMAGE_EXTENSIONS):
                    targets.append(os.path.join(current, name))
    return targets


def main():
    parser = argparse.ArgumentParser(description='MangaDock 封面体检与修复')
    parser.add_argument('--apply', action='store_true', help='实际修复（默认只体检）')
    parser.add_argument('--root', action='append', default=None,
                        help='扫描目录，可重复；默认 static/cover 及其子目录')
    parser.add_argument('--backup-dir', default=None, help='修复前的备份目录')
    parser.add_argument('--repo', default=REPO_ROOT, help='MangaDock 仓库根目录')
    args = parser.parse_args()

    roots = args.root or list(DEFAULT_ROOTS)
    targets = collect_files(roots, os.path.abspath(args.repo))

    print(f'扫描目录: {", ".join(roots)}')
    print(f'待检图片: {len(targets)} 个\n')

    broken = []
    for path in targets:
        healthy, note = describe(path)
        if not healthy:
            broken.append((path, note))

    if not broken:
        print('全部正常，无需修复。')
        return 0

    print(f'发现 {len(broken)} 个问题文件:')
    for path, note in broken:
        print(f'  [{note}]  {os.path.relpath(path, os.path.abspath(args.repo))}')

    if not args.apply:
        print('\n当前为体检模式，未改动任何文件。确认后加 --apply 执行修复。')
        return 1

    backup_dir = args.backup_dir or os.path.join(
        os.path.expanduser('~'), '.mangadock', 'cover-backups',
        time.strftime('%Y%m%d-%H%M%S'),
    )
    os.makedirs(backup_dir, exist_ok=True)
    print(f'\n备份目录: {backup_dir}')

    fixed = failed = 0
    for path, note in broken:
        flat_name = os.path.relpath(path, os.path.abspath(args.repo)).replace(os.sep, '__')
        try:
            shutil.copy2(path, os.path.join(backup_dir, flat_name))
        except Exception as exc:
            print(f'  跳过（备份失败）{flat_name}: {exc}')
            failed += 1
            continue

        expected = expected_format_for_path(path)
        if normalize_cover_file(path) and is_readable_image(path, expected):
            with Image.open(path) as image:
                print(f'  ✓ 已修复 {os.path.basename(path)} → {image.format} {image.size}')
            fixed += 1
        else:
            print(f'  ✗ 无法转码 {os.path.basename(path)}（{note}）—— 原文件保持不动')
            failed += 1

    print(f'\n完成：修复 {fixed} 个，失败 {failed} 个')
    if failed:
        print('失败项多为源文件损坏或本机缺少对应解码器，可按需手工替换封面。')
    return 0 if fixed else 1


if __name__ == '__main__':
    sys.exit(main())
