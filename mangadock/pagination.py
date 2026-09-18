# -*- coding: utf-8 -*-
"""Small pagination helpers for in-memory library collections."""


def paginate_sequence(items, requested_page, per_page=24):
    """Return one page of ``items`` and presentation-friendly metadata."""
    items = list(items)
    try:
        page = int(requested_page or 1)
    except (TypeError, ValueError):
        page = 1

    page = max(1, page)
    total_items = len(items)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page

    candidates = {1, total_pages}
    candidates.update(range(max(1, page - 2), min(total_pages, page + 2) + 1))
    page_links = []
    previous = None
    for candidate in sorted(candidates):
        if previous is not None and candidate - previous > 1:
            page_links.append(None)
        page_links.append(candidate)
        previous = candidate

    return items[start:start + per_page], {
        'page': page,
        'per_page': per_page,
        'total_items': total_items,
        'total_pages': total_pages,
        'page_links': page_links,
        'has_prev': page > 1,
        'has_next': page < total_pages,
        'prev_page': page - 1,
        'next_page': page + 1,
    }
