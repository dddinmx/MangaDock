# -*- coding: utf-8 -*-
"""Cover upscaling for large Hero displays.

Uses high-quality Lanczos resampling + unsharp mask (no heavy ML dependency).
Enhanced files are cached under static/cover/hero/ and regenerated when the
source cover is newer.
"""
import os
import threading

from PIL import Image, ImageFilter

from mangadock.settings import COVER_ROOT

# Longest edge target for hero covers (fills large banners without insane files)
HERO_TARGET_EDGE = int(os.environ.get('MANGADOCK_COVER_HERO_EDGE', '1920'))
# If source already larger than this, only re-encode + mild sharpen
HERO_MIN_EDGE = int(os.environ.get('MANGADOCK_COVER_HERO_MIN_EDGE', '1400'))
HERO_JPEG_QUALITY = int(os.environ.get('MANGADOCK_COVER_HERO_QUALITY', '92'))

HERO_DIR = os.path.join(COVER_ROOT, 'hero')

_lock = threading.Lock()


def source_cover_path(comic_name):
    if not comic_name:
        return None
    return os.path.join(COVER_ROOT, f'{comic_name}.jpg')


def hero_cover_path(comic_name):
    if not comic_name:
        return None
    return os.path.join(HERO_DIR, f'{comic_name}.jpg')


def _resample_filter():
    return getattr(getattr(Image, 'Resampling', Image), 'LANCZOS')


def _prepare_rgb(image):
    if image.mode in ('RGB',):
        return image
    if image.mode == 'L':
        return image.convert('RGB')
    return image.convert('RGB')


def _upscale_image(image):
    """Upscale so the longer edge reaches HERO_TARGET_EDGE when needed."""
    width, height = image.size
    longest = max(width, height)
    if longest <= 0:
        return image

    if longest >= HERO_MIN_EDGE:
        # Already large enough for full-bleed; optional mild upscale if below target
        if longest >= HERO_TARGET_EDGE:
            return image
        scale = HERO_TARGET_EDGE / float(longest)
    else:
        scale = HERO_TARGET_EDGE / float(longest)

    # Cap scale to avoid absurd memory on tiny icons (e.g. 50px -> 38x)
    scale = min(scale, 6.0)
    if scale <= 1.05:
        return image

    new_size = (
        max(1, int(round(width * scale))),
        max(1, int(round(height * scale))),
    )
    return image.resize(new_size, _resample_filter())


def _sharpen(image):
    # Mild unsharp: helps "felt" clarity after upscale without halos
    try:
        return image.filter(ImageFilter.UnsharpMask(radius=1.4, percent=140, threshold=2))
    except Exception:
        return image.filter(ImageFilter.SHARPEN)


def enhance_cover_file(source_path, dest_path):
    """Read source cover, upscale/sharpen, write dest. Returns dest_path or None."""
    if not source_path or not os.path.isfile(source_path):
        return None

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    try:
        with Image.open(source_path) as image:
            image.load()
            image = _prepare_rgb(image)
            image = _upscale_image(image)
            image = _sharpen(image)
            image.save(
                dest_path,
                'JPEG',
                quality=HERO_JPEG_QUALITY,
                optimize=True,
                progressive=True,
            )
        return dest_path
    except Exception as exc:
        try:
            print(f'封面超分失败: {source_path} -> {exc}')
        except Exception:
            pass
        return None


def ensure_hero_cover(comic_name, force=False):
    """
    Ensure hero cache exists for comic_name.
    Returns absolute path to hero jpg, or source path if enhance fails, or None.
    """
    source = source_cover_path(comic_name)
    if not source or not os.path.isfile(source):
        return None

    dest = hero_cover_path(comic_name)
    if not dest:
        return None

    with _lock:
        if not force and os.path.isfile(dest):
            try:
                if os.path.getmtime(dest) >= os.path.getmtime(source):
                    return dest
            except OSError:
                pass

        result = enhance_cover_file(source, dest)
        if result:
            return result
        return source


def refresh_hero_cover(comic_name):
    """Force regenerate hero cover after a new download/upload."""
    return ensure_hero_cover(comic_name, force=True)
