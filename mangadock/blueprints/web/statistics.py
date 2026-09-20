# -*- coding: utf-8 -*-
"""阅读统计页。"""
from datetime import datetime, timedelta

from flask import render_template, request, session, url_for

from mangadock.auth import login_required
from mangadock.core import app
from mangadock.services.novels import NOVEL_PROGRESS_PREFIX, get_novels
from mangadock.services.reading import (
    get_reading_time_by_comic,
    get_reading_time_daily_for_year,
    get_reading_time_monthly_for_year,
    get_total_reading_time,
)
from mangadock.settings import china_tz


@app.route('/statistics')
@login_required
def statistics():
    """Reading statistics page"""
    stats_year = datetime.now(china_tz).year
    current_user_id = session.get('user_id')
    total_time = get_total_reading_time(current_user_id)
    reading_time_rank = get_reading_time_by_comic(current_user_id)
    reading_time_monthly = get_reading_time_monthly_for_year(stats_year, current_user_id)
    reading_time_daily = get_reading_time_daily_for_year(stats_year, current_user_id)

    novels_by_id = {novel['novel_id']: novel for novel in get_novels()}
    max_rank_duration = max(
        (int(entry.get('total_duration') or 0) for entry in reading_time_rank),
        default=0,
    )
    ranking_items = []
    for entry in reading_time_rank:
        source_name = entry.get('comic_name') or ''
        total_duration = int(entry.get('total_duration') or 0)
        item = {
            'title': source_name,
            'subtitle': '漫画',
            'media_type': '漫画',
            'cover_url': url_for('static', filename=f'cover/{source_name}.jpg'),
            'total_duration': total_duration,
            'ratio': (total_duration / max_rank_duration * 100) if max_rank_duration else 0,
        }

        if source_name.startswith(NOVEL_PROGRESS_PREFIX):
            novel_id = source_name[len(NOVEL_PROGRESS_PREFIX):]
            novel = novels_by_id.get(novel_id)
            item.update({
                'title': novel['title'] if novel else novel_id,
                'subtitle': novel['author'] if novel else '小说',
                'media_type': '小说',
                'cover_url': (
                    url_for('novel_cover', novel_id=novel_id, v=novel['cover_version'])
                    if novel else ''
                ),
            })
        ranking_items.append(item)

    # Process monthly data for chart rendering
    monthly_data = {
        'months': ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'],
        'durations': [0] * 12  # Initialize with 0 for all months
    }

    for entry in reading_time_monthly:
        month = entry[0]  # Format: YYYY-MM
        duration = entry[1]  # minutes
        month_num = int(month.split('-')[1]) - 1  # Convert to 0-based index
        if 0 <= month_num < 12:
            monthly_data['durations'][month_num] = duration

    # Calculate max duration for chart scaling
    max_duration = max(monthly_data['durations']) if monthly_data['durations'] else 1

    daily_duration_map = {
        entry[0]: int(entry[1] or 0)
        for entry in reading_time_daily
    }
    max_daily_duration = max(daily_duration_map.values()) if daily_duration_map else 0

    year_start = datetime(stats_year, 1, 1).date()
    year_end = datetime(stats_year, 12, 31).date()
    grid_start = year_start - timedelta(days=year_start.weekday())
    grid_end = year_end + timedelta(days=(6 - year_end.weekday()))
    today = datetime.now(china_tz).date()

    month_labels = []
    heatmap_weeks = []
    cursor = grid_start
    week_index = 0

    while cursor <= grid_end:
        week_cells = []
        week_month_label = ''

        for day_offset in range(7):
            current_day = cursor + timedelta(days=day_offset)
            iso_day = current_day.isoformat()
            duration = daily_duration_map.get(iso_day, 0)

            if duration <= 0 or max_daily_duration <= 0:
                level = 0
            else:
                ratio = duration / max_daily_duration
                if ratio <= 0.25:
                    level = 1
                elif ratio <= 0.5:
                    level = 2
                elif ratio <= 0.75:
                    level = 3
                else:
                    level = 4

            if current_day.day == 1 and current_day.month <= 12:
                week_month_label = f"{current_day.month}月"
            elif week_index == 0 and day_offset == 0:
                week_month_label = '1月'

            week_cells.append({
                'date': iso_day,
                'day': current_day.day,
                'duration': duration,
                'level': level,
                'is_current_year': current_day.year == stats_year,
                'is_today': current_day == today
            })

        month_labels.append(week_month_label)
        heatmap_weeks.append(week_cells)
        cursor += timedelta(days=7)
        week_index += 1

    return render_template(
        'statistics.html',
        library_mode=(request.args.get('mode') if request.args.get('mode') in {'comic', 'novel'} else 'comic'),
        stats_year=stats_year,
        total_time=total_time,
        reading_time_rank=ranking_items,
        monthly_data=monthly_data,
        max_duration=max_duration,
        heatmap_weeks=heatmap_weeks,
        heatmap_month_labels=month_labels,
        max_daily_duration=max_daily_duration
    )
