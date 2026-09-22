# -*- coding: utf-8 -*-
"""封面超分：给大屏 Hero / 高 DPI 卡片提供足够分辨率的封面缓存。

背景
----
源站给的封面普遍很小（实测 82 张里宽度中位数 320px，最小的只有 180×240），
而主页 hero 的显示宽度是 1267px（1920 视口）→ 浏览器把 320px 拉成 4 倍，
糊得一眼可见。

策略
----
落盘时在 ``static/cover/hero/<漫画名>.jpg`` 生成一份超分缓存，长边
``MANGADOCK_COVER_HERO_EDGE``（默认 1920）。两级引擎：

1. **AI 超分（可选）** —— 检测到可用引擎时优先使用，走**子进程**调用，
   绝不在 Flask 进程里 import torch（避免几百 MB 常驻内存 + 阻塞 worker）。
   引擎优先级：``onnx``（随镜像打包，onnxruntime 纯 CPU 多线程，无需 torch）
   > ``python``（torch + spandrel，本机部署约定）> ``ncnn`` 系（外部可执行体）。
   - onnx：``<当前解释器> worker.py --input --output --model <repo>/tools/models/*.onnx``
   - ncnn 系（``realesrgan-ncnn-vulkan`` / ``upscayl-bin``）：
     ``bin -i <in> -o <out> -s <scale> [-n <model>]``
   - 通用 worker：``$MANGADOCK_COVER_SR_PYTHON worker.py --input --output --scale``
2. **Pillow 兜底（永远可用）** —— 分步 Lanczos（每步 ≤2×，抑制一次拉 6 倍的
   振铃）+ 双半径 unsharp。质量不如模型，但比浏览器双线性好很多，且零依赖。

缓存何时生成
------------
- 入库时：封面落盘后调 :func:`refresh_hero_cover`（三个 provider + 上传封面都接了）。
- 漏网的旧封面：主页渲染时 :func:`request_hero_cover` 起一个后台线程补生成，
  本次先用原图返回——**绝不在请求线程里做超分**。
- 批量补：``tools/refresh_hero_covers.py``。

环境变量
--------
``MANGADOCK_COVER_SR``            auto(默认) / off / onnx / python / ncnn
``MANGADOCK_COVER_SR_BIN``        指定可执行体路径（默认在 PATH 里找）
``MANGADOCK_COVER_SR_PYTHON``     通用 worker 的 python
``MANGADOCK_COVER_SR_WORKER``     通用 worker 脚本路径
``MANGADOCK_COVER_SR_MODEL``      覆盖模型文件路径（.onnx 或 .pth）
``MANGADOCK_COVER_HERO_EDGE``     目标长边，默认 1920
``MANGADOCK_COVER_HERO_MIN_EDGE`` 超过这个长边就不放大，只锐化，默认 1400
``MANGADOCK_COVER_HERO_QUALITY``  JPEG 质量，默认 92
``MANGADOCK_COVER_SR_TIMEOUT``    单个封面超分超时（秒），默认 180
"""
import atexit
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

from PIL import Image, ImageFilter

from mangadock.settings import COVER_ROOT

# 长边目标：一屏 hero 在 2560 宽下需要 ~1700px，留点余量
HERO_TARGET_EDGE = int(os.environ.get('MANGADOCK_COVER_HERO_EDGE', '1920'))
# 源图长边达到这个值就不再放大（只补锐化），避免无谓的计算
HERO_MIN_EDGE = int(os.environ.get('MANGADOCK_COVER_HERO_MIN_EDGE', '1400'))
HERO_JPEG_QUALITY = int(os.environ.get('MANGADOCK_COVER_HERO_QUALITY', '92'))
# 单张超分超时（AI 引擎在 CPU 上跑 4× 可能十几秒）
SR_TIMEOUT_SECONDS = int(os.environ.get('MANGADOCK_COVER_SR_TIMEOUT', '180'))
# 引擎选择：auto / off / onnx / python / ncnn
SR_ENGINE = (os.environ.get('MANGADOCK_COVER_SR', 'auto') or 'auto').strip().lower()

HERO_DIR = os.path.join(COVER_ROOT, 'hero')

_lock = threading.Lock()
# 正在后台补生成的漫画名，避免同一张封面被并发排队
_pending = set()
_pending_lock = threading.Lock()

# 运行中的 SR 子进程 PID（仅在 _run_sr_engine 拿到 PID 引用处登记），用于进程退出时
# 兜底 SIGTERM，避免 gunicorn worker 被重启杀死后 SR 子进程变孤儿、继续占 CPU/内存。
_sr_pids = set()
_sr_pids_lock = threading.Lock()

# 后台补生成并发上限：每个 hero 线程会跑 AI 引擎（CPU 密集、十几秒），不加限流会在
# 模板渲染期（首页几十张未就绪封面）瞬间起几十个线程抢算力，整体反而更慢且可能 OOM。
# 用信号量把同时运行的 SR 后台线程压到 4 个；拿不到许可就丢弃本次——反正 hero 未就绪
# 本来就会返回源图，下次请求再补即可。
_SR_BACKGROUND_SEMAPHORE = threading.Semaphore(4)


def _track_sr_process(pid):
    """登记一个正在运行的 SR 子进程 PID。"""
    if pid is None:
        return
    with _sr_pids_lock:
        _sr_pids.add(pid)


def _untrack_sr_process(pid):
    """SR 子进程结束后注销其 PID。"""
    if pid is None:
        return
    with _sr_pids_lock:
        _sr_pids.discard(pid)


def _atexit_cleanup_sr():
    """进程退出时，对仍存活的 SR 子进程发 SIGTERM，避免孤儿进程残留。

    仅在拿到 PID 引用时生效（简单可靠）：正常结束时 _run_sr_engine 的 finally 已注销；
    若进程在 SR 运行中被杀，atexit 仍能兜底清理仍登记的 PID。
    """
    with _sr_pids_lock:
        pids = list(_sr_pids)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass  # 已退出则忽略


atexit.register(_atexit_cleanup_sr)


# ────────────────────────── 路径 ──────────────────────────

def source_cover_path(comic_name):
    if not comic_name:
        return None
    return os.path.join(COVER_ROOT, f'{comic_name}.jpg')


def hero_cover_path(comic_name):
    if not comic_name:
        return None
    return os.path.join(HERO_DIR, f'{comic_name}.jpg')


def hero_cover_ready(comic_name):
    """缓存存在**且不比源图旧**才算可用（源封面换了要重新生成）。"""
    source = source_cover_path(comic_name)
    dest = hero_cover_path(comic_name)
    if not source or not dest:
        return False
    try:
        if not os.path.isfile(dest):
            return False
        return os.path.getmtime(dest) >= os.path.getmtime(source)
    except OSError:
        return False


# ────────────────────────── 外部 AI 超分引擎 ──────────────────────────

def _resolve_ncnn_bin():
    """找一个可用的 ncnn 系超分可执行体。"""
    explicit = (os.environ.get('MANGADOCK_COVER_SR_BIN') or '').strip()
    if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
        return explicit
    for name in ('realesrgan-ncnn-vulkan', 'upscayl-bin', 'waifu2x-ncnn-vulkan'):
        found = shutil.which(name)
        if found:
            return found
    return None


def _resolve_python_worker():
    """通用 worker：解释器 + 脚本都有才可用。

    默认落点（本机部署约定，避免在 Web 进程里 import torch）：
      python:  ``~/Developer/mangadock-sr-venv/bin/python``
      worker:  ``<repo>/tools/cover_sr_worker.py``
      模型:    由 worker 自己找 ``~/Developer/mangadock-sr/models/*.pth``
    三者都不进仓库/发布树，缺任何一个就自动退回 Pillow 链路。
    """
    python = (os.environ.get('MANGADOCK_COVER_SR_PYTHON') or '').strip()
    worker = (os.environ.get('MANGADOCK_COVER_SR_WORKER') or '').strip()

    if not python:
        candidate = os.path.expanduser('~/Developer/mangadock-sr-venv/bin/python')
        if os.path.isfile(candidate):
            python = candidate
    if not worker:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        candidate = os.path.join(repo_root, 'tools', 'cover_sr_worker.py')
        if os.path.isfile(candidate):
            worker = candidate

    if python and worker and os.path.isfile(python) and os.path.isfile(worker):
        return python, worker
    return None, None


def _resolve_onnx():
    """打包 ONNX 引擎：模型随镜像/仓库分发，worker 用当前解释器跑 onnxruntime。

    三者齐全（模型文件 + worker 脚本 + 当前环境装了 onnxruntime）才可用。
    """
    model = (os.environ.get('MANGADOCK_COVER_SR_MODEL') or '').strip()
    if not model or not os.path.isfile(model):
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        model = None
        models_dir = os.path.join(repo_root, 'tools', 'models')
        if os.path.isdir(models_dir):
            names = [n for n in sorted(os.listdir(models_dir))
                     if n.lower().endswith('.onnx') and not n.startswith('._')]
            # anime / illustration 系列优先
            names.sort(key=lambda n: (0 if 'anime' in n.lower() else 1, n))
            if names:
                model = os.path.join(models_dir, names[0])
    if not model:
        return None

    worker = (os.environ.get('MANGADOCK_COVER_SR_WORKER') or '').strip()
    if not worker:
        candidate = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), 'tools', 'cover_sr_worker.py')
        if os.path.isfile(candidate):
            worker = candidate
    if not worker or not os.path.isfile(worker):
        return None

    try:
        import importlib.util
        if importlib.util.find_spec('onnxruntime') is None:
            return None
    except ImportError:
        return None

    return model, worker


def sr_engine_name():
    """返回当前会自动使用的引擎名：onnx / python / ncnn / pillow。"""
    if SR_ENGINE == 'off':
        return 'pillow'
    if SR_ENGINE in ('auto', 'onnx'):
        resolved = _resolve_onnx()
        if resolved:
            return 'onnx'
    if SR_ENGINE in ('auto', 'python'):
        python, worker = _resolve_python_worker()
        if python and worker:
            return 'python'
    if SR_ENGINE in ('auto', 'ncnn') and _resolve_ncnn_bin():
        return 'ncnn'
    return 'pillow'


def _engine_guard():
    """全局（跨进程）串行闸：AI 推理用的是同一块 GPU(MPS)，8 个 gunicorn
    worker 同时跑 8 张只会互相抢算力、整体更慢。这里让机器上同一时刻只有
    一张封面在跑模型；Pillow 链路（0.3s）不占用这个闸。

    返回锁句柄表示拿到闸；返回 None 表示**锁不可用**——此时调用方必须降级为
    Pillow 兜底，**绝不能跑 AI 引擎**（否则失去串行闸，多个进程会并发抢算力）。
    """
    import fcntl

    path = os.path.join(_lock_dir(), '_sr_engine.lock')
    try:
        os.makedirs(_lock_dir(), exist_ok=True)
        handle = open(path, 'w')
    except OSError as exc:
        # .locks 目录不可写（只读/权限问题）：锁不可用，告警并降级 Pillow，
        # 异常不向上抛（调用方拿不到锁句柄即走兜底）。
        print(f'封面超分全局闸不可用（{_lock_dir()} 无法创建/写入），'
              f'降级 Pillow 兜底: {exc}')
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError as exc:
        try:
            handle.close()
        except OSError:
            pass
        print(f'封面超分全局闸 flock 失败，降级 Pillow 兜底: {exc}')
        return None
    return handle


def _run_sr_engine(source_path, out_path, scale):
    """用外部引擎把 source 放大 scale 倍写到 out_path；失败返回 False（回落 Pillow）。"""
    engine = sr_engine_name()
    if engine == 'pillow':
        return False

    command = None
    if engine == 'onnx':
        resolved = _resolve_onnx()
        if resolved:
            model, worker = resolved
            command = [sys.executable, worker, '--input', source_path,
                       '--output', out_path, '--model', model,
                       '--scale', str(int(scale))]
    elif engine == 'ncnn':
        binary = _resolve_ncnn_bin()
        if binary:
            command = [binary, '-i', source_path, '-o', out_path, '-s', str(int(scale))]
    elif engine == 'python':
        python, worker = _resolve_python_worker()
        if python and worker:
            command = [python, worker, '--input', source_path,
                       '--output', out_path, '--scale', str(int(scale))]

    if not command:
        return False

    # 锁不可用 → 明确不跑 AI 引擎，直接回落 Pillow（告警已在 _engine_guard 内打印）。
    guard = _engine_guard()
    if guard is None:
        return False

    proc = None
    try:
        # 用 Popen 而非 run：拿到子进程 PID 以便进程退出时兜底 SIGTERM，避免孤儿进程。
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _track_sr_process(proc.pid)
        try:
            _stdout, stderr = proc.communicate(timeout=SR_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            # 超时：先杀子进程再清理，避免其变孤儿继续占资源。
            # 注意 communicate 超时时元组解包未完成，stderr 取异常自带的属性。
            _kill_sr_process(proc)
            err = (exc.stderr or b'').decode('utf-8', 'replace').strip()
            print(f'封面超分引擎超时（{engine}, >{SR_TIMEOUT_SECONDS}s）: '
                  f'{source_path}' + (f' | {err}' if err else ''))
            return False

        if proc.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
            err = stderr.decode('utf-8', 'replace').strip() if stderr else ''
            print(f'封面超分引擎返回异常（{engine}, rc={proc.returncode}）: '
                  f'{source_path}' + (f' | {err}' if err else ''))
            return False
        return True
    except Exception as exc:
        print(f'封面超分引擎调用失败（{engine}）: {source_path} -> {exc}')
        if proc is not None:
            _kill_sr_process(proc)
        return False
    finally:
        _untrack_sr_process(proc.pid if proc else None)
        _release_lock(guard)


def _kill_sr_process(proc):
    """杀掉 SR 子进程：先 SIGTERM，5s 不退则 SIGKILL 兜底。"""
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ────────────────────────── Pillow 兜底链路 ──────────────────────────

def _resample_filter():
    return getattr(getattr(Image, 'Resampling', Image), 'LANCZOS')


def _prepare_rgb(image):
    if image.mode == 'RGB':
        return image
    return image.convert('RGB')


def _stepwise_resize(image, new_size):
    """分步缩放：单步倍率控制在 2× 以内，比一次性拉 6 倍干净得多。"""
    current = image
    for _ in range(8):
        width, height = current.size
        if width <= 0 or height <= 0:
            return current
        factor = max(new_size[0] / float(width), new_size[1] / float(height))
        if factor <= 1.02:
            return current
        step = min(factor, 2.0)
        step_size = (
            max(1, int(round(width * step))),
            max(1, int(round(height * step))),
        )
        if step_size == current.size:
            return current
        # 中间步用 LANCZOS，最后一步若正好命中目标则一步到位
        current = current.resize(step_size, _resample_filter())
        if current.size == new_size:
            return current
        if current.size[0] >= new_size[0] and current.size[1] >= new_size[1]:
            return current.resize(new_size, _resample_filter())
    return current


def _target_size(width, height):
    longest = max(width, height)
    if longest <= 0:
        return None
    if longest >= HERO_TARGET_EDGE:
        return None
    scale = min(HERO_TARGET_EDGE / float(longest), 6.0)
    if scale <= 1.05:
        return None
    return (max(1, int(round(width * scale))), max(1, int(round(height * scale))))


def _sharpen(image, strong=True):
    """双半径 unsharp：细半径找回线条，粗半径补一点微反差。"""
    try:
        image = image.filter(ImageFilter.UnsharpMask(
            radius=1.3, percent=150 if strong else 70, threshold=2))
        if strong:
            image = image.filter(ImageFilter.UnsharpMask(
                radius=2.8, percent=55, threshold=3))
        return image
    except Exception:
        try:
            return image.filter(ImageFilter.SHARPEN)
        except Exception:
            return image


def _save_jpeg(image, dest_path):
    """原子写：先写同目录临时文件再 os.replace，杜绝半截缓存被静态服务读走。"""
    directory = os.path.dirname(dest_path) or '.'
    os.makedirs(directory, exist_ok=True)
    file_descriptor, temp_path = tempfile.mkstemp(prefix='.hero-', suffix='.jpg', dir=directory)
    os.close(file_descriptor)
    try:
        image.save(
            temp_path,
            'JPEG',
            quality=HERO_JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )
        with Image.open(temp_path) as probe:
            probe.load()
        os.replace(temp_path, dest_path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def enhance_cover_file(source_path, dest_path, force_pillow=False):
    """读源封面 → 超分 → 锐化 → 写 dest。成功返回 dest_path，否则 None。

    force_pillow: 为 True 时跳过 AI 引擎，只用 Pillow 兜底（用于跨进程锁不可用的降级场景）。
    """
    if not source_path or not os.path.isfile(source_path):
        return None

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    try:
        with Image.open(source_path) as image:
            image.load()
            image = _prepare_rgb(image)
            width, height = image.size
            target = _target_size(width, height)
            longest = float(max(width, height))
            real_factor = HERO_TARGET_EDGE / longest if longest > 0 else 1.0

            used_engine = 'pillow'
            # 实际选用的引擎（force_pillow 时强制 Pillow）
            engine = 'pillow' if force_pillow else sr_engine_name()

            # 是否值得跑 AI 引擎：
            #  - 放大倍率 < 1.4x：Pillow 分步 Lanczos 已足够清晰，且避免大图撞超时；
            #  - onnx 模型固定 4x：若真实所需倍率 < 2x，跑 4x 再缩回纯属“放大-缩小”空转，
            #    直接走 Pillow（_stepwise_resize 会精确缩到目标尺寸）。
            use_ai = (engine != 'pillow' and target is not None
                      and real_factor >= 1.4
                      and not (engine == 'onnx' and real_factor < 2.0))

            if use_ai:
                # ① 先试 AI 引擎（整数倍放大即可，之后统一缩到目标尺寸）
                scale = max(2, min(4, int(round(real_factor))))
                # 临时目录用 with 上下文管理器（等价 try/finally）确保退出即清理；
                # 进程正常退出时 TemporaryDirectory 自身也会 atexit 兜底清理。
                with tempfile.TemporaryDirectory(prefix='cover-sr-') as temp_dir:
                    sr_out = os.path.join(temp_dir, 'sr.png')
                    if _run_sr_engine(source_path, sr_out, scale):
                        try:
                            with Image.open(sr_out) as sr_image:
                                sr_image.load()
                                image = _prepare_rgb(sr_image)
                            used_engine = engine
                        except Exception as exc:
                            print(f'封面超分产物无法读取: {sr_out} -> {exc}')

            # ② 无论如何都归一到目标尺寸（Pillow 兜底也走这条，避免只锐化不放大）
            if target is not None and image.size != target:
                image = _stepwise_resize(image, target)

            # ③ 锐化：AI 产物已经很锐，只做很轻的一道；Pillow 链路正常力度
            image = _sharpen(image, strong=(used_engine == 'pillow'))

            _save_jpeg(image, dest_path)
        return dest_path
    except Exception as exc:
        try:
            print(f'封面超分失败: {source_path} -> {exc}')
        except Exception:
            pass
        return None


# ────────────────────────── 对外 API ──────────────────────────

def _lock_dir():
    return os.path.join(HERO_DIR, '.locks')


# 跨进程锁不可用时（.locks 目录只读/权限问题）返回的哨兵，与“已被其它进程持有”的
# None 区分开：前者应降级 Pillow 兜底仍生成缓存，后者应正常跳过本次。
_CROSS_LOCK_UNAVAILABLE = object()


def _cross_process_lock(comic_name):
    """跨进程互斥（gunicorn 多 worker 会同时想去生成同一张）。

    返回锁句柄表示拿到；返回 None 表示已被其它进程持有（正常跳过）；
    返回 ``_CROSS_LOCK_UNAVAILABLE`` 表示锁文件不可用（目录只读/权限），
    调用方应降级为 Pillow 兜底仍生成缓存，而不是静默跳过。
    """
    import fcntl

    directory = _lock_dir()
    # 锁文件名用 comic_name 的 sha1 前缀，避免 ``A/B`` 与 ``A:B`` 因路径分隔符/规范化
    # 碰撞到同一把锁（原名还会因含 / 直接 open 失败），也避免异常字符进文件名。
    lock_name = hashlib.sha1(comic_name.encode('utf-8')).hexdigest()[:16]
    try:
        os.makedirs(directory, exist_ok=True)
        handle = open(os.path.join(directory, f'{lock_name}.lock'), 'w')
    except OSError as exc:
        # .locks 目录不可写：锁不可用，告警并返回哨兵，异常不向上抛。
        print(f'封面超分跨进程锁不可用（{directory} 无法写入），'
              f'降级 Pillow 兜底: {exc}')
        return _CROSS_LOCK_UNAVAILABLE
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # 已被其它进程持有：正常跳过，本次不重复劳动。
        handle.close()
        return None
    return handle


def _release_lock(handle):
    import fcntl

    if handle is None:
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass


def _cache_is_fresh(dest, source):
    try:
        return os.path.isfile(dest) and os.path.getmtime(dest) >= os.path.getmtime(source)
    except OSError:
        return False


def ensure_hero_cover(comic_name, force=False):
    """确保 hero 缓存存在。返回缓存绝对路径；失败时返回源封面路径或 None。"""
    source = source_cover_path(comic_name)
    if not source or not os.path.isfile(source):
        return None

    dest = hero_cover_path(comic_name)
    if not dest:
        return None

    # 进程内串行：一次只跑一张（AI 引擎单张 7~11s，并发只会互相抢 CPU）
    with _lock:
        if not force and _cache_is_fresh(dest, source):
            return dest

        lock_handle = _cross_process_lock(comic_name)
        if lock_handle is _CROSS_LOCK_UNAVAILABLE:
            # 跨进程锁不可用（.locks 只读/权限）：降级为 Pillow 兜底仍生成缓存，
            # 不跑昂贵的 AI 引擎，但封面照常服务。
            print(f'封面超分锁不可用，降级 Pillow 兜底: {comic_name}')
            if not force and _cache_is_fresh(dest, source):
                return dest
            result = enhance_cover_file(source, dest, force_pillow=True)
            return result if result else source

        if lock_handle is None:
            # 别的进程正在做同一张，本次不重复劳动
            return dest if os.path.isfile(dest) else source

        try:
            if not force and _cache_is_fresh(dest, source):
                return dest
            result = enhance_cover_file(source, dest)
            if result:
                return result
            return source
        finally:
            _release_lock(lock_handle)


def refresh_hero_cover(comic_name):
    """兼容保留：入库链路现用 :func:`request_hero_cover`（按需后台补生成），
    本函数不再被任何调用方使用，仅保留以免破坏外部脚本/历史引用。"""
    return ensure_hero_cover(comic_name, force=True)


def request_hero_cover(comic_name):
    """请求后台补生成（不阻塞请求线程）。已在排队/已可用则直接返回。

    并发上限由模块级 ``_SR_BACKGROUND_SEMAPHORE``（4）控制：拿不到许可就丢弃本次，
    反正 hero 未就绪本来就会返回源图，下次请求再补——避免在模板渲染期瞬间起几十个
    SR 线程抢算力、整体更慢甚至 OOM。
    """
    if not comic_name or hero_cover_ready(comic_name):
        return False

    # 非阻塞抢后台并发许可：满 4 个在跑就直接放弃本次补生成。
    if not _SR_BACKGROUND_SEMAPHORE.acquire(blocking=False):
        return False

    with _pending_lock:
        if comic_name in _pending:
            # 已在排队：归还许可，避免信号量被空占。
            _SR_BACKGROUND_SEMAPHORE.release()
            return False
        _pending.add(comic_name)

    def _worker():
        try:
            ensure_hero_cover(comic_name)
        except Exception as exc:
            print(f'封面超分后台任务失败: {comic_name} -> {exc}')
        finally:
            with _pending_lock:
                _pending.discard(comic_name)
            _SR_BACKGROUND_SEMAPHORE.release()

    threading.Thread(target=_worker, name=f'cover-hero-{comic_name[:16]}', daemon=True).start()
    return True


def backfill_hero_covers(force=False, on_progress=None):
    """把 static/cover 下所有漫画封面补一遍超分缓存。

    返回 (生成数, 跳过数, 失败数)。
    """
    if not os.path.isdir(COVER_ROOT):
        return 0, 0, 0

    done = skipped = failed = 0
    names = []
    for entry in sorted(os.listdir(COVER_ROOT)):
        if entry.startswith('.') or not entry.lower().endswith(('.jpg', '.jpeg', '.png')):
            continue
        if entry.lower() == 'cover.png':
            continue
        names.append(os.path.splitext(entry)[0])

    total = len(names)
    for index, name in enumerate(names, start=1):
        if not force and hero_cover_ready(name):
            skipped += 1
        else:
            if ensure_hero_cover(name, force=True):
                done += 1
            else:
                failed += 1
        if on_progress:
            try:
                on_progress(index, total, name)
            except Exception:
                pass
    return done, skipped, failed
