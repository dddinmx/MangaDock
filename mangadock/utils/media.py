# -*- coding: utf-8 -*-
"""Image/PDF conversion helpers."""
import glob
import os
import re
import shutil
import subprocess
import tempfile

import img2pdf
from natsort import natsorted
from PIL import Image

from mangadock.settings import CONFIG, PDF_TOOL_TIMEOUT_SECONDS


def safe_print(message, end="\n", flush=False):
    print(message, end=end, flush=flush)

def default_headers(referer=None):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    if referer:
        headers['Referer'] = referer
    return headers


def sanitize_filename(name):
    safe_name = re.sub(r'[\\/:*?"<>|]', '', (name or '').strip())
    return safe_name or "untitled"


def ensure_directory(path):
    os.makedirs(path, exist_ok=True)


def build_pdf_from_image_list(image_paths, output_path):
    """Normalize images and build a PDF whose page size matches each image."""
    max_size = 14400
    min_size = 3

    with tempfile.TemporaryDirectory(prefix="pdf-build-", dir=os.path.dirname(output_path)) as temp_dir:
        normalized_paths = []

        for index, image_path in enumerate(natsorted(image_paths), start=1):
            with Image.open(image_path) as img:
                if img.mode not in ('RGB', 'L'):
                    img = img.convert('RGB')
                elif img.mode == 'L':
                    img = img.convert('RGB')

                width, height = img.size
                scale = 1.0

                if width > max_size:
                    scale = min(scale, max_size / width)
                if height > max_size:
                    scale = min(scale, max_size / height)
                if width < min_size:
                    scale = max(scale, min_size / width)
                if height < min_size:
                    scale = max(scale, min_size / height)

                if scale != 1.0:
                    width = max(min(int(round(width * scale)), max_size), min_size)
                    height = max(min(int(round(height * scale)), max_size), min_size)
                    img = img.resize((width, height), Image.Resampling.LANCZOS)

                normalized_path = os.path.join(temp_dir, f"{index:04d}.jpg")
                img.save(normalized_path, "JPEG", quality=95)
                normalized_paths.append(normalized_path)

        if not normalized_paths:
            return False, "没有有效的图片可以生成PDF"

        pdf_bytes = img2pdf.convert(normalized_paths)
        with open(output_path, "wb") as output_file:
            output_file.write(pdf_bytes)

    return True, f"成功生成PDF：{output_path}"


def get_pdf_page_size(pdf_path):
    try:
        result = subprocess.run(
            ["pdfinfo", pdf_path],
            capture_output=True,
            text=True,
            check=True,
            timeout=PDF_TOOL_TIMEOUT_SECONDS
        )
        match = re.search(r'Page size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts', result.stdout)
        if match:
            return float(match.group(1)), float(match.group(2))
    except Exception:
        return None, None
    return None, None


def pdf_needs_repair(pdf_path):
    width, height = get_pdf_page_size(pdf_path)
    if width is None or height is None:
        return False
    return width <= 10 or height <= 10


def repair_pdf_for_reading(pdf_path):
    """Repair previously generated malformed PDFs by extracting embedded images."""
    if not pdf_path.lower().endswith('.pdf') or not os.path.exists(pdf_path):
        return pdf_path
    if not pdf_needs_repair(pdf_path):
        return pdf_path

    repaired_dir = os.path.join(os.path.dirname(pdf_path), ".repaired")
    repaired_path = os.path.join(repaired_dir, os.path.basename(pdf_path))
    ensure_directory(repaired_dir)

    if os.path.exists(repaired_path) and not pdf_needs_repair(repaired_path):
        return repaired_path

    try:
        with tempfile.TemporaryDirectory(prefix="pdf-repair-", dir=repaired_dir) as temp_dir:
            prefix = os.path.join(temp_dir, "page")
            subprocess.run(
                ["pdfimages", "-png", pdf_path, prefix],
                check=True,
                capture_output=True,
                text=True,
                timeout=PDF_TOOL_TIMEOUT_SECONDS
            )
            extracted_images = natsorted(glob.glob(os.path.join(temp_dir, "page-*.png")))
            if not extracted_images:
                return pdf_path

            success, _ = build_pdf_from_image_list(extracted_images, repaired_path)
            if success and os.path.exists(repaired_path) and not pdf_needs_repair(repaired_path):
                return repaired_path
    except Exception as exc:
        safe_print(f"修复PDF失败: {exc}")

    return pdf_path

