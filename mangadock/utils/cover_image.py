# -*- coding: utf-8 -*-
"""封面图片格式归一化。

背景：源站有时会把 AVIF / HEIF / WebP 等格式的图片挂在 ``.jpg`` 名称下返回。
旧实现把响应字节原样写入 ``static/cover/<name>.jpg``，于是静态服务按扩展名
下发 ``Content-Type: image/jpeg``，而实际内容是别的格式。

Chrome / Firefox 会嗅探图片内容、照样渲染，但 **Safari / WebKit 按声明的
MIME 类型挑解码器**：声明 image/jpeg 却塞 WebP/AVIF 数据会直接解码失败，
前端 ``onerror`` 兜底换成默认封面，表现为「明明有封面却显示默认图」。

实测同一批封面在 iOS Safari 全部回落，而文件本身用 ImageIO 能正常解码，
说明问题出在类型不匹配而非文件损坏。

这里统一在落盘前做一次「解码 → 转码 → 写回」：

1. Pillow 能解的（JPEG/PNG/WEBP/GIF/BMP/TIFF）直接走 Pillow；
2. Pillow 不认的（AVIF/HEIF）依次尝试 ``pillow_heif`` / ``pillow_avif``
   → macOS ``sips`` → ImageMagick → ffmpeg；
3. 全部失败则**不写盘**（保留原有封面），绝不留下一个名实不符的坏文件。

所有写入都走「临时文件 + ``os.replace``」，保证不会出现半截文件。
"""
import importlib
import os
import shutil
import subprocess
import tempfile
from io import BytesIO

from PIL import Image

from mangadock.utils.media import ensure_directory, safe_print

# 外部转换工具的超时（秒）
CONVERTER_TIMEOUT_SECONDS = int(os.environ.get('MANGADOCK_COVER_CONVERT_TIMEOUT', '60'))
# 落盘图片的最长边上限（只缩不放，防止异常大图）
DEFAULT_MAX_EDGE = int(os.environ.get('MANGADOCK_COVER_MAX_EDGE', '2400'))
# 落盘 JPEG 质量
DEFAULT_QUALITY = int(os.environ.get('MANGADOCK_COVER_QUALITY', '92'))

# 扩展名 → 该文件实际应写入的格式（保持文件名与内容一致）
OUTPUT_FORMATS = {
    '.jpg': 'JPEG',
    '.jpeg': 'JPEG',
    '.png': 'PNG',
    '.webp': 'WEBP',
}

_MAGIC_PREFIXES = (
    (b'\xff\xd8\xff', 'JPEG'),
    (b'\x89PNG\r\n\x1a\n', 'PNG'),
    (b'GIF87a', 'GIF'),
    (b'GIF89a', 'GIF'),
    (b'BM', 'BMP'),
    (b'II*\x00', 'TIFF'),
    (b'MM\x00*', 'TIFF'),
)

# ISO-BMFF 品牌 → 格式名（AVIF/HEIF 都装在这个容器里）
_ISOBMFF_BRANDS = {
    b'avif': 'AVIF',
    b'avis': 'AVIF',
    b'heic': 'HEIF',
    b'heix': 'HEIF',
    b'hevc': 'HEIF',
    b'hevx': 'HEIF',
    b'mif1': 'HEIF',
    b'msf1': 'HEIF',
}

_optional_plugins_checked = False


def sniff_image_format(content):
    """按文件头判断真实格式，无法识别时返回 None。"""
    if not content or len(content) < 12:
        return None

    for magic, name in _MAGIC_PREFIXES:
        if content.startswith(magic):
            return name

    if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return 'WEBP'

    if content[4:8] == b'ftyp':
        brand = content[8:12]
        return _ISOBMFF_BRANDS.get(brand, brand.decode('ascii', 'ignore') or 'ISO-BMFF')

    return None


def expected_format_for_path(path):
    """按扩展名推断该文件「应该」是什么格式；未知扩展名按 JPEG 处理。"""
    return OUTPUT_FORMATS.get(os.path.splitext(path or '')[1].lower(), 'JPEG')


def _lanczos():
    return getattr(getattr(Image, 'Resampling', Image), 'LANCZOS')


def _ensure_optional_plugins():
    """尝试注册 Pillow 的 AVIF/HEIF 插件，返回是否有插件可用。"""
    global _optional_plugins_checked

    registered = False
    for module_name in ('pillow_heif', 'pillow_avif'):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        if module_name == 'pillow_heif' and hasattr(module, 'register_heif_opener'):
            try:
                module.register_heif_opener()
            except Exception:
                pass
        registered = True

    _optional_plugins_checked = True
    return registered


def _open_bytes(content):
    try:
        image = Image.open(BytesIO(content))
        image.load()
        return image
    except Exception:
        return None


def _read_image_copy(path):
    """读回转换产物并钉在内存里，避免临时目录回收后文件失效。"""
    try:
        with open(path, 'rb') as handle:
            payload = handle.read()
        image = Image.open(BytesIO(payload))
        image.load()
        return image
    except Exception:
        return None


def _external_converters(source_path, output_path, quality):
    """依次尝试可用的外部转换工具，返回 (工具名, Image) 。"""
    attempts = []

    sips = shutil.which('sips')
    if sips:
        attempts.append(('sips', [
            sips, '-s', 'format', 'jpeg', '-s', 'formatOptions', str(quality),
            source_path, '--out', output_path,
        ]))

    magick = shutil.which('magick')
    if magick:
        attempts.append(('magick', [magick, source_path, '-quality', str(quality), output_path]))

    convert = shutil.which('convert')
    if convert:
        attempts.append(('convert', [convert, source_path, '-quality', str(quality), output_path]))

    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        attempts.append(('ffmpeg', [
            ffmpeg, '-y', '-loglevel', 'error', '-i', source_path,
            '-q:v', '3', output_path,
        ]))

    for name, command in attempts:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=CONVERTER_TIMEOUT_SECONDS,
            )
        except Exception:
            continue

        if result.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            image = _read_image_copy(output_path)
            if image is not None:
                return name, image

        # 失败产物清掉，避免干扰下一次尝试
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass

    return None, None


def _decode_content(content, source_format):
    """把原始字节解码成 Pillow Image，失败返回 None。"""
    image = _open_bytes(content)
    if image is not None:
        return image

    if not _optional_plugins_checked:
        _ensure_optional_plugins()
        image = _open_bytes(content)
        if image is not None:
            return image

    hint = (source_format or 'bin').lower()
    with tempfile.TemporaryDirectory(prefix='cover-decode-') as temp_dir:
        source_path = os.path.join(temp_dir, f'source.{hint}')
        output_path = os.path.join(temp_dir, 'output.jpg')
        with open(source_path, 'wb') as handle:
            handle.write(content)

        tool_name, image = _external_converters(source_path, output_path, DEFAULT_QUALITY)

    if image is not None:
        safe_print(f'封面转码：{source_format or "未知格式"} → JPEG（{tool_name}）')
    return image


def _write_image(image, dest_path, image_format='JPEG', max_edge=None, quality=None):
    """按指定格式原子写入图片，成功返回 True。

    JPEG 会先转 RGB（不支持 alpha）；PNG 保留原模式。
    """
    limit = max_edge or DEFAULT_MAX_EDGE
    try:
        if image_format == 'JPEG' and image.mode != 'RGB':
            image = image.convert('RGB')

        width, height = image.size
        if width <= 0 or height <= 0:
            return False

        longest = max(width, height)
        if limit and longest > limit:
            scale = limit / float(longest)
            image = image.resize(
                (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                _lanczos(),
            )

        directory = os.path.dirname(dest_path)
        if directory:
            ensure_directory(directory)

        suffix = os.path.splitext(dest_path)[1].lower() or '.jpg'
        file_descriptor, temp_path = tempfile.mkstemp(
            prefix='.cover-', suffix=suffix, dir=directory or '.'
        )
        os.close(file_descriptor)

        try:
            if image_format == 'JPEG':
                image.save(
                    temp_path,
                    'JPEG',
                    quality=quality or DEFAULT_QUALITY,
                    optimize=True,
                )
            else:
                image.save(temp_path, image_format)

            # 写盘后回读校验，坏文件不覆盖旧封面
            with Image.open(temp_path) as probe:
                probe.load()
            os.replace(temp_path, dest_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        return True
    except Exception as exc:
        safe_print(f'图片写盘失败: {dest_path} -> {exc}')
        return False


def normalize_cover_bytes(content, dest_path, max_edge=None, quality=None):
    """把封面字节统一转成 JPEG 并落盘，无法解码时不写盘。

    下载落盘专用：目标文件名固定是 ``<漫画名>.jpg``，所以一律输出 JPEG。
    返回 True 表示 dest_path 已写入合法 JPEG。
    """
    if not content:
        return False

    source_format = sniff_image_format(content)
    image = _decode_content(content, source_format)
    if image is None:
        safe_print(f'封面转码放弃（无法解码，格式 {source_format or "未知"}）: {dest_path}')
        return False

    return _write_image(image, dest_path, 'JPEG', max_edge=max_edge, quality=quality)


def normalize_image_bytes(content, dest_path, max_edge=None, quality=None):
    """归一化图片字节，输出格式按 dest_path 的扩展名决定。

    用于修既有文件：``.jpg`` 输出 JPEG、``.png`` 输出 PNG，保持名实一致。
    """
    if not content:
        return False

    image_format = expected_format_for_path(dest_path)
    source_format = sniff_image_format(content)
    image = _decode_content(content, source_format)
    if image is None:
        safe_print(f'图片转码放弃（无法解码，格式 {source_format or "未知"}）: {dest_path}')
        return False

    return _write_image(image, dest_path, image_format, max_edge=max_edge, quality=quality)


def normalize_cover_file(source_path, dest_path=None, max_edge=None, quality=None):
    """就地（或另存）归一化一个图片文件，返回 True 表示已产出格式正确的图片。"""
    if not source_path or not os.path.isfile(source_path):
        return False

    try:
        with open(source_path, 'rb') as handle:
            content = handle.read()
    except OSError as exc:
        safe_print(f'图片读取失败: {source_path} -> {exc}')
        return False

    return normalize_image_bytes(
        content,
        dest_path or source_path,
        max_edge=max_edge,
        quality=quality,
    )


def is_readable_image(path, image_format=None):
    """判断文件是否是可正常解码的图片；给定 image_format 时同时校验格式。"""
    try:
        with Image.open(path) as image:
            if image_format and image.format != image_format:
                return False
            image.load()
        return True
    except Exception:
        return False


def is_readable_jpeg(path):
    """判断文件是否是浏览器能稳定解码的 JPEG。"""
    return is_readable_image(path, 'JPEG')
