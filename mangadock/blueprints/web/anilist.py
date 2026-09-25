"""AniList account connection and explicit comic matching."""
from urllib.parse import urlencode

from flask import flash, jsonify, redirect, render_template, request, url_for

from mangadock.auth import get_current_user, login_required, require_csrf_for_session_auth
from mangadock.core import app
from mangadock.extensions import csrf, db
from mangadock.models import AniListAccount, AniListComicLink
from mangadock.services.anilist import CLIENT_ID, chapter_number, connect, link_manga, search_manga, sync_completed_chapter
from mangadock.services.library import get_comic_directory, list_local_chapters
from mangadock.services.reading import user_can_access_progress_key


@app.route('/anilist/connect', methods=['GET', 'POST'])
@login_required
def anilist_connect():
    if request.method == 'POST':
        try:
            account = connect(get_current_user().id, request.form.get('access_token', ''))
            flash(f'已连接 AniList：{account.username}')
            return redirect(url_for('settings'))
        except ValueError:
            db.session.rollback()
            flash('访问令牌无效，请重新从 AniList 复制')
        except Exception:
            db.session.rollback()
            app.logger.exception('AniList PIN authorization failed')
            flash('AniList 连接失败，请稍后重试')
    authorize_url = 'https://anilist.co/api/v2/oauth/authorize?' + urlencode({
        'client_id': CLIENT_ID,
        'response_type': 'token',
    })
    return render_template('anilist_connect.html', authorize_url=authorize_url)


@app.route('/anilist/disconnect', methods=['POST'])
@login_required
def anilist_disconnect():
    user_id = get_current_user().id
    AniListComicLink.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    account = db.session.get(AniListAccount, user_id)
    if account:
        db.session.delete(account)
    db.session.commit()
    flash('已断开 AniList，漫画关联已清除')
    return redirect(url_for('settings'))


@app.route('/anilist/match/<path:comic_name>')
@login_required
def anilist_match(comic_name):
    user = get_current_user()
    user_id = user.id
    if not user_can_access_progress_key(comic_name, user) or not get_comic_directory(comic_name):
        return render_template('error.html', message='漫画不存在或无权访问'), 404
    account = db.session.get(AniListAccount, user_id)
    link = AniListComicLink.query.filter_by(user_id=user_id, comic_name=comic_name).first()
    query = (request.args.get('q') or comic_name).strip()[:100]
    results = []
    if account and query:
        try:
            results = search_manga(query)
        except Exception:
            app.logger.exception('AniList manga search failed')
            flash('AniList 搜索暂时失败，请稍后重试')
    chapters = list_local_chapters(comic_name)
    suggested_first_chapter = chapter_number(chapters[0].get('title')) if chapters else None
    return render_template('anilist_match.html', comic_name=comic_name, account=account,
                           link=link, query=query, results=results,
                           chapter_count=len(chapters), suggested_first_chapter=suggested_first_chapter or 1)


@app.route('/anilist/match/<path:comic_name>', methods=['POST'])
@login_required
def anilist_save_match(comic_name):
    user = get_current_user()
    user_id = user.id
    if not user_can_access_progress_key(comic_name, user) or not get_comic_directory(comic_name):
        return render_template('error.html', message='漫画不存在或无权访问'), 404
    if not db.session.get(AniListAccount, user_id):
        flash('请先连接 AniList')
        return redirect(url_for('settings'))
    try:
        media_id = int(request.form.get('media_id', ''))
        first_chapter = int(request.form.get('first_chapter', ''))
        if media_id <= 0 or first_chapter < 1 or first_chapter > 100000:
            raise ValueError('无效的漫画或章节号')
        link_manga(user_id, comic_name, media_id, first_chapter)
        flash('AniList 作品关联已保存；之后读完章节才会同步')
    except (TypeError, ValueError):
        flash('请选择有效的漫画条目和首章章节号')
    except Exception:
        db.session.rollback()
        app.logger.exception('AniList manga matching failed')
        flash('AniList 关联失败，请稍后重试')
    return redirect(url_for('anilist_match', comic_name=comic_name))


@app.route('/anilist/unmatch/<path:comic_name>', methods=['POST'])
@login_required
def anilist_unmatch(comic_name):
    user = get_current_user()
    user_id = user.id
    if not user_can_access_progress_key(comic_name, user):
        return render_template('error.html', message='漫画不存在或无权访问'), 404
    AniListComicLink.query.filter_by(user_id=user_id, comic_name=comic_name).delete(synchronize_session=False)
    db.session.commit()
    flash('已取消 AniList 关联')
    return redirect(url_for('anilist_match', comic_name=comic_name))


@app.route('/anilist/complete', methods=['POST'])
@login_required
@csrf.exempt
def anilist_complete():
    csrf_error = require_csrf_for_session_auth(api_response=True)
    if csrf_error:
        return csrf_error
    data = request.get_json(silent=True) or {}
    comic_name = data.get('comic_name')
    user = get_current_user()
    user_id = user.id
    if not user_can_access_progress_key(comic_name, user):
        return jsonify({'error': '无权访问该漫画'}), 403
    try:
        index = int(data.get('chapter_index'))
    except (TypeError, ValueError):
        return jsonify({'error': '无效的章节'}), 400
    chapters = list_local_chapters(comic_name)
    if index < 0 or index >= len(chapters):
        return jsonify({'error': '章节不存在'}), 400
    try:
        changed = sync_completed_chapter(user_id, comic_name, index)
        return jsonify({'success': True, 'updated': changed})
    except Exception:
        db.session.rollback()
        app.logger.exception('AniList chapter sync failed')
        return jsonify({'error': 'AniList 同步失败，请稍后重试'}), 502
