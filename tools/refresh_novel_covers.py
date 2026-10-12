#!/usr/bin/env python
"""Check or backfill enhanced covers for all existing local EPUB novels."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mangadock.services.novels import ensure_novel_cover, get_novels  # noqa: E402
from mangadock.utils.cover_enhance import sr_engine_name  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description='小说封面超分检查 / 补算')
    parser.add_argument('--apply', action='store_true', help='生成缺失的高清封面')
    args = parser.parse_args()
    novels = get_novels()
    missing = [novel for novel in novels if novel['cover_version'].endswith('-source')]
    print(f'小说 {len(novels)} 本，待处理 {len(missing)} 本；引擎：{sr_engine_name()}', flush=True)
    if not args.apply:
        return int(bool(missing))
    failed = []
    for index, novel in enumerate(missing, 1):
        try:
            result = ensure_novel_cover(novel['novel_id'])
        except Exception as exc:
            print(f'处理失败：{novel["title"]}：{exc}', flush=True)
            result = None
        if not result:
            failed.append(novel['novel_id'])
        print(f'[{index}/{len(missing)}] {novel["title"]}：'
              f'{"完成" if result else "无封面或处理失败"}', flush=True)
    print(f'处理完成：成功 {len(missing) - len(failed)}，失败/无封面 {len(failed)}', flush=True)
    return int(bool(failed))


if __name__ == '__main__':
    sys.exit(main())
