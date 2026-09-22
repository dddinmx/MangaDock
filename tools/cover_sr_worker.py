#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""封面超分 worker（独立进程，供 MangaDock 主进程以子进程方式调用）。

为什么单独一个进程：torch 一 import 就是几百 MB 常驻内存，Flask/gunicorn 的
worker 不该背这个包袱；而且模型推理可能跑十几秒，放在 Web 进程里会占住 worker。
主进程只做 subprocess 调用，失败就回落到 Pillow 链路（见 utils/cover_enhance.py）。

用法::

    python sr_worker.py --input in.jpg --output out.png [--scale 4] [--model 名称]

输出统一是 PNG（无损，交给主进程做最后的定尺 + JPEG 编码）。

模型搜索路径（按顺序）：
  1. ``--model`` 给的绝对路径
  2. ``$MANGADOCK_COVER_SR_MODEL``
  3. ``$MANGADOCK_COVER_SR_MODEL_DIR/*``（.pth 与 .onnx，多模型时插画系优先）
  4. ``<repo>/tools/models/*``（随镜像分发的打包模型）
  5. ``~/Developer/mangadock-sr/models/*.pth``（本机默认落点）

推理后端按模型后缀自动选：``.onnx`` → onnxruntime（纯 CPU 多线程，
不需要 torch）；``.pth`` → torch + spandrel。
多模型时**插画系（anime）优先**——漫画封面是描边插画，通用模型会把线条抹软。
"""
import argparse
import os
import sys
import time


def build_parser():
    parser = argparse.ArgumentParser(description='Real-ESRGAN 封面超分 worker')
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--scale', type=int, default=4,
                        help='期望放大倍数；onnx/torch 后端模型固定输出 4x，此值仅用于日志/选路，'
                             '实际产物恒为 4x（主进程会再缩到目标尺寸）')
    parser.add_argument('--model', default='', help='模型 .pth 路径，留空则自动查找')
    parser.add_argument('--tile', type=int, default=256, help='分块边长，0 = 不分块')
    parser.add_argument('--tile-overlap', type=int, default=16)
    parser.add_argument('--device', default='auto', help='auto / mps / cpu')
    parser.add_argument('--quiet', action='store_true')
    return parser


def log(message, quiet=False):
    if not quiet:
        print(f'[sr-worker] {message}', flush=True)


def _model_dirs():
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = []
    env_dir = (os.environ.get('MANGADOCK_COVER_SR_MODEL_DIR') or '').strip()
    if env_dir:
        dirs.append(env_dir)
    # 打包在仓库里的模型（Docker 镜像里走这条）
    dirs.append(os.path.join(here, 'models'))
    dirs.append(os.path.expanduser('~/Developer/mangadock-sr/models'))
    dirs.append(os.path.join(here, '..', 'models'))
    return dirs


def find_model(explicit=''):
    candidates = []
    if explicit:
        candidates.append(explicit)
    env_model = (os.environ.get('MANGADOCK_COVER_SR_MODEL') or '').strip()
    if env_model:
        candidates.append(env_model)

    for folder in _model_dirs():
        if not os.path.isdir(folder):
            continue
        names = [n for n in sorted(os.listdir(folder))
                 if n.lower().endswith(('.pth', '.onnx'))
                 and not n.startswith('._')]
        # anime / illustration 系列优先
        names.sort(key=lambda n: (0 if 'anime' in n.lower() else 1, n))
        candidates.extend(os.path.join(folder, n) for n in names)

    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    raise SystemExit(
        '找不到超分模型（--model / $MANGADOCK_COVER_SR_MODEL / '
        '$MANGADOCK_COVER_SR_MODEL_DIR / ~/Developer/mangadock-sr/models 都没有）'
    )


def pick_device(requested):
    import torch
    wanted = (requested or 'auto').lower()
    if wanted == 'cpu':
        return torch.device('cpu')
    if wanted == 'mps':
        if not torch.backends.mps.is_available():
            raise SystemExit('指定了 mps 但本机不可用')
        return torch.device('mps')
    if torch.backends.mps.is_available():
        return torch.device('mps')
    return torch.device('cpu')


def load_model(model_path, device):
    from spandrel import ModelLoader
    descriptor = ModelLoader().load_from_file(model_path)
    descriptor.model.eval()
    descriptor.model.to(device)
    return descriptor


def run_tensor(descriptor, tensor):
    """跑模型；MPS 上个别算子不支持时回落 CPU 重跑一次。"""
    import torch
    with torch.no_grad():
        try:
            return descriptor.model(tensor)
        except Exception as exc:  # MPS 上 pixel_unshuffle 等算子偶发不支持
            if tensor.device.type != 'mps':
                raise
            print(f'[sr-worker] MPS 推理失败，回落 CPU: {exc}', file=sys.stderr, flush=True)
            descriptor.model.to('cpu')
            return descriptor.model(tensor.to('cpu'))


def upscale(image, descriptor, device, tile, overlap, quiet=False):
    import numpy as np
    import torch

    original_mode = image.mode
    rgb = image.convert('RGB')
    width, height = rgb.size
    array = np.asarray(rgb, dtype=np.float32) / 255.0
    tensor = torch.from_numpy(array).permute(2, 0, 1).unsqueeze(0).to(device)

    use_tile = tile and tile > 0 and (width > tile or height > tile)
    if not use_tile:
        output = run_tensor(descriptor, tensor)
    else:
        output = torch.zeros(
            (1, 3, height * 4, width * 4), dtype=torch.float32, device=tensor.device
        )
        step = max(1, tile - overlap)
        for top in range(0, height, step):
            bottom = min(top + tile, height)
            top = max(0, bottom - tile)
            for left in range(0, width, step):
                right = min(left + tile, width)
                left = max(0, right - tile)
                patch = tensor[:, :, top:bottom, left:right]
                patch_out = run_tensor(descriptor, patch)
                oh = (bottom - top) * 4
                ow = (right - left) * 4
                patch_out = patch_out[:, :, :oh, :ow]
                output[:, :, top * 4:bottom * 4, left * 4:right * 4] = patch_out
            if bottom >= height:
                break
        output = output.cpu()

    output = output.squeeze(0).clamp(0, 1).permute(1, 2, 0).cpu().numpy()
    result = (output * 255.0 + 0.5).astype('uint8')
    from PIL import Image as PILImage
    restored = PILImage.fromarray(result, 'RGB')
    if original_mode == 'L':
        restored = restored.convert('L')
    return restored


# ────────────────────────── ONNX 路径（无需 torch） ──────────────────────────

def upscale_onnx(image, session, tile, overlap, quiet=False):
    """onnxruntime 推理。和 torch 路径同一套分块逻辑，纯 numpy。"""
    import numpy as np

    original_mode = image.mode
    rgb = image.convert('RGB')
    width, height = rgb.size
    array = np.asarray(rgb, dtype=np.float32) / 255.0
    inp = array.transpose(2, 0, 1)[None]
    input_name = session.get_inputs()[0].name

    def run(arr):
        out = session.run(None, {input_name: arr})[0]
        return out[0]

    use_tile = tile and tile > 0 and (width > tile or height > tile)
    if not use_tile:
        out = run(inp)
    else:
        out = np.zeros((3, height * 4, width * 4), dtype=np.float32)
        step = max(1, tile - overlap)
        for top in range(0, height, step):
            bottom = min(top + tile, height)
            top = max(0, bottom - tile)
            for left in range(0, width, step):
                right = min(left + tile, width)
                left = max(0, right - tile)
                patch_out = run(inp[:, :, top:bottom, left:right])
                out[:, top * 4:bottom * 4, left * 4:right * 4] = patch_out[
                    :, :(bottom - top) * 4, :(right - left) * 4
                ]
            if bottom >= height:
                break

    result = (out.transpose(1, 2, 0).clip(0, 1) * 255.0 + 0.5).astype('uint8')
    from PIL import Image as PILImage
    restored = PILImage.fromarray(result, 'RGB')
    if original_mode == 'L':
        restored = restored.convert('L')
    return restored


def load_onnx_session(model_path):
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(
        model_path, sess_options=options, providers=['CPUExecutionProvider']
    )


def main():
    args = build_parser().parse_args()

    from PIL import Image
    if not os.path.isfile(args.input):
        raise SystemExit(f'输入不存在: {args.input}')

    started = time.time()
    model_path = find_model(args.model)
    log(f'模型 {os.path.basename(model_path)}', args.quiet)

    with Image.open(args.input) as image:
        image.load()
        source_size = image.size
        width, height = source_size

        # OOM 防护：输出缓冲按 h*4 × w*4 全量分配，超大图会直接爆内存。
        # 最长边 > 1600px 时源图已经很大、超分收益也低，直接返回原图不放大
        # （1600 × 4 = 6400 上限可控），由主进程按 Pillow 链路兜底。
        if max(width, height) > 1600:
            log(f'源图过大（{width}x{height}，最长边 > 1600），跳过超分直接返回原图',
                args.quiet)
            os.makedirs(os.path.dirname(os.path.abspath(args.output)) or '.', exist_ok=True)
            image.save(args.output, 'PNG')
            return 0

        if model_path.lower().endswith('.onnx'):
            # ONNX 路径：只依赖 onnxruntime（Docker 镜像走的就是这条）。
            # 注意：onnx 模型固定输出 4x，--scale 仅作日志/选路，产物恒为 4x，
            # 最终由主进程 cover_enhance.py 缩到目标尺寸（目标倍率 < 2x 时直接跳过本路径走 Pillow）。
            try:
                session = load_onnx_session(model_path)
            except Exception as exc:
                raise SystemExit(f'加载 ONNX 模型失败: {exc}')
            log('后端 onnxruntime (CPU)', args.quiet)
            result = upscale_onnx(image, session, args.tile, args.tile_overlap, args.quiet)
        else:
            try:
                device = pick_device(args.device)
            except Exception as exc:
                raise SystemExit(str(exc))
            descriptor = load_model(model_path, device)
            log(f'设备 {device} · 模型原生倍率 ×{getattr(descriptor, "scale", "?")}', args.quiet)
            result = upscale(image, descriptor, device, args.tile, args.tile_overlap, args.quiet)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or '.', exist_ok=True)
    result.save(args.output, 'PNG')
    log(f'{source_size[0]}x{source_size[1]} → {result.size[0]}x{result.size[1]}'
        f'，用时 {time.time() - started:.2f}s', args.quiet)
    return 0


if __name__ == '__main__':
    sys.exit(main())
