# -*- coding: utf-8 -*-
"""Filesystem path helpers."""
import os


def resolve_file_under_directory(base_dir, relative_filename):
    if not base_dir or not relative_filename:
        return None
    absolute_base = os.path.realpath(os.path.abspath(base_dir))
    candidate = os.path.realpath(os.path.abspath(os.path.join(absolute_base, relative_filename)))
    if candidate != absolute_base and candidate.startswith(absolute_base + os.sep):
        return candidate
    return None

