/**
 * Shared helper for MangaDock /api/v1 JSON endpoints.
 * Expects session cookie auth + X-CSRFToken for write methods.
 *
 * Forms can opt in with data-api-v1="<action>" and optional data-user-id / data-scan-id.
 */
(function (global) {
    'use strict';

    function getCsrfToken(explicitToken) {
        if (explicitToken) {
            return explicitToken;
        }
        if (typeof global.csrfToken === 'string' && global.csrfToken) {
            return global.csrfToken;
        }
        var input = document.querySelector('input[name="csrf_token"]');
        return input ? input.value : '';
    }

    function parseJsonSafe(response) {
        return response.text().then(function (text) {
            if (!text) {
                return null;
            }
            try {
                return JSON.parse(text);
            } catch (error) {
                return null;
            }
        });
    }

    function errorMessage(payload, fallback) {
        if (!payload) {
            return fallback || '请求失败';
        }
        if (payload.error && payload.error.message) {
            return payload.error.message;
        }
        if (payload.message) {
            return payload.message;
        }
        return fallback || '请求失败';
    }

    /**
     * @param {string} method
     * @param {string} path absolute path e.g. /api/v1/downloads
     * @param {object|FormData|null} body
     * @param {object} options { csrfToken }
     */
    function apiV1(method, path, body, options) {
        options = options || {};
        var headers = {
            Accept: 'application/json',
        };
        var upper = (method || 'GET').toUpperCase();
        var init = {
            method: upper,
            credentials: 'include',
            headers: headers,
        };

        if (upper !== 'GET' && upper !== 'HEAD') {
            headers['X-CSRFToken'] = getCsrfToken(options.csrfToken);
            if (body !== undefined && body !== null && !(body instanceof FormData)) {
                headers['Content-Type'] = 'application/json';
                init.body = JSON.stringify(body);
            } else if (body instanceof FormData) {
                init.body = body;
            } else if (upper === 'POST' || upper === 'PUT' || upper === 'PATCH' || upper === 'DELETE') {
                headers['Content-Type'] = 'application/json';
                init.body = JSON.stringify({});
            }
        }

        return fetch(path, init).then(function (response) {
            return parseJsonSafe(response).then(function (payload) {
                var ok = !!(payload && payload.ok === true);
                return {
                    ok: ok,
                    status: response.status,
                    data: payload && payload.data !== undefined ? payload.data : null,
                    error: payload && payload.error ? payload.error : null,
                    message: errorMessage(payload, response.statusText || '请求失败'),
                    raw: payload,
                    response: response,
                };
            });
        });
    }

    function formatClock(value) {
        if (!value) {
            return '--:--:--';
        }
        if (/^\d{1,2}:\d{2}(:\d{2})?$/.test(value)) {
            return value.length === 5 ? value + ':00' : value;
        }
        var date = new Date(value);
        if (!isNaN(date.getTime())) {
            return date.toLocaleTimeString('zh-CN', { hour12: false });
        }
        return String(value);
    }

    function formValue(form, name) {
        var el = form.elements.namedItem(name);
        if (!el) {
            return '';
        }
        // Don't trim passwords; other fields can be trimmed.
        var raw = el.value || '';
        if (name && name.indexOf('password') !== -1) {
            return raw;
        }
        return String(raw).trim();
    }

    function checkedValues(form, name) {
        var nodes = form.querySelectorAll('input[name="' + name + '"]:checked');
        var values = [];
        nodes.forEach(function (node) {
            values.push(node.value);
        });
        return values;
    }

    function setFormBusy(form, busy) {
        var buttons = form.querySelectorAll('button, input[type="submit"]');
        buttons.forEach(function (button) {
            button.disabled = !!busy;
        });
    }

    function finishSuccess(form, result) {
        var message = (result.data && result.data.message) || result.message || '操作成功';
        var reload = form.getAttribute('data-api-reload');
        var redirect = form.getAttribute('data-api-redirect');

        if (redirect) {
            window.location.href = redirect;
            return;
        }
        if (reload === '0' || reload === 'false') {
            if (message) {
                alert(message);
            }
            return;
        }
        window.location.reload();
    }

    function buildRequest(form) {
        var action = form.getAttribute('data-api-v1');
        var csrfToken = getCsrfToken(formValue(form, 'csrf_token'));

        switch (action) {
            case 'group-create':
                return {
                    method: 'POST',
                    path: '/api/v1/groups',
                    body: { name: formValue(form, 'group_name') },
                    csrfToken: csrfToken,
                };
            case 'group-delete':
                return {
                    method: 'DELETE',
                    path: '/api/v1/groups/' + encodeURIComponent(formValue(form, 'group_name')),
                    body: {},
                    csrfToken: csrfToken,
                };
            case 'group-assign':
                return {
                    method: 'POST',
                    path: '/api/v1/groups/assign',
                    body: {
                        comic_name: formValue(form, 'comic_name'),
                        group_name: formValue(form, 'group_name'),
                    },
                    csrfToken: csrfToken,
                };
            case 'hidden-comic':
                return {
                    method: 'POST',
                    path: '/api/v1/library/hidden/comics',
                    body: {
                        comic_name: formValue(form, 'comic_name'),
                        action: formValue(form, 'action'),
                    },
                    csrfToken: csrfToken,
                };
            case 'hidden-group':
                return {
                    method: 'POST',
                    path: '/api/v1/library/hidden/groups',
                    body: {
                        group_name: formValue(form, 'group_name'),
                        action: formValue(form, 'action'),
                    },
                    csrfToken: csrfToken,
                };
            case 'scan-add':
                return {
                    method: 'POST',
                    path: '/api/v1/settings/scan-paths',
                    body: { path: formValue(form, 'scan_path') },
                    csrfToken: csrfToken,
                };
            case 'scan-delete':
                return {
                    method: 'DELETE',
                    path: '/api/v1/settings/scan-paths/' + encodeURIComponent(form.getAttribute('data-scan-id') || ''),
                    body: {},
                    csrfToken: csrfToken,
                };
            case 'scan-rescan':
                return {
                    method: 'POST',
                    path: '/api/v1/settings/scan-paths/rescan',
                    body: {},
                    csrfToken: csrfToken,
                };
            case 'user-create':
                return {
                    method: 'POST',
                    path: '/api/v1/users',
                    body: {
                        username: formValue(form, 'username'),
                        password: formValue(form, 'password'),
                        confirm_password: formValue(form, 'confirm_password'),
                        role: formValue(form, 'role') || 'user',
                        group_names: checkedValues(form, 'group_names'),
                    },
                    csrfToken: csrfToken,
                };
            case 'user-delete':
                return {
                    method: 'DELETE',
                    path: '/api/v1/users/' + encodeURIComponent(form.getAttribute('data-user-id') || ''),
                    body: {},
                    csrfToken: csrfToken,
                };
            case 'user-groups':
                return {
                    method: 'PUT',
                    path: '/api/v1/users/' + encodeURIComponent(form.getAttribute('data-user-id') || '') + '/groups',
                    body: { group_names: checkedValues(form, 'group_names') },
                    csrfToken: csrfToken,
                };
            case 'cover-upload': {
                var fileInput = form.querySelector('input[type="file"][name="cover_file"]');
                if (!fileInput || !fileInput.files || !fileInput.files[0]) {
                    throw new Error('请选择封面图片');
                }
                var comicName = form.getAttribute('data-comic-name') || formValue(form, 'comic_name');
                var fd = new FormData();
                fd.append('cover_file', fileInput.files[0]);
                return {
                    method: 'POST',
                    path: '/api/v1/comics/by-name/' + encodeURIComponent(comicName) + '/cover',
                    body: fd,
                    csrfToken: csrfToken,
                };
            }
            case 'change-password':
                return {
                    method: 'POST',
                    path: '/api/v1/account/password',
                    body: {
                        old_password: formValue(form, 'old_password'),
                        new_password: formValue(form, 'new_password'),
                        confirm_password: formValue(form, 'confirm_password'),
                    },
                    csrfToken: csrfToken,
                    redirectOnSuccess: form.getAttribute('data-api-redirect') || '/login',
                };
            case 'login':
                return {
                    method: 'POST',
                    path: '/api/v1/auth/login',
                    body: {
                        username: formValue(form, 'username'),
                        password: formValue(form, 'password'),
                        next: formValue(form, 'next') || form.getAttribute('data-next') || '',
                    },
                    csrfToken: csrfToken,
                    useDataNext: true,
                };
            case 'logout':
                return {
                    method: 'POST',
                    path: '/api/v1/auth/logout',
                    body: {},
                    csrfToken: csrfToken,
                    redirectOnSuccess: form.getAttribute('data-api-redirect') || '/login',
                };
            default:
                return null;
        }
    }

    function bindApiForms(root) {
        var scope = root || document;
        scope.addEventListener('submit', function (event) {
            var form = event.target;
            if (!(form instanceof HTMLFormElement)) {
                return;
            }
            if (!form.hasAttribute('data-api-v1')) {
                return;
            }

            event.preventDefault();
            if (form.dataset.apiBusy === '1') {
                return;
            }

            var confirmMessage = form.getAttribute('data-api-confirm');
            if (confirmMessage && !window.confirm(confirmMessage)) {
                return;
            }

            var requestSpec;
            try {
                requestSpec = buildRequest(form);
            } catch (error) {
                alert(error.message || '表单无效');
                return;
            }
            if (!requestSpec) {
                alert('未知的 API 表单类型');
                return;
            }

            form.dataset.apiBusy = '1';
            setFormBusy(form, true);

            apiV1(requestSpec.method, requestSpec.path, requestSpec.body, { csrfToken: requestSpec.csrfToken })
                .then(function (result) {
                    if (!result.ok) {
                        var errorTarget = form.getAttribute('data-api-error-target');
                        var errorNode = errorTarget ? document.querySelector(errorTarget) : null;
                        if (errorNode) {
                            errorNode.textContent = result.message || '操作失败';
                            errorNode.classList.remove('hidden');
                            errorNode.style.display = 'block';
                        } else {
                            alert(result.message || '操作失败');
                        }
                        return;
                    }
                    if (requestSpec.useDataNext && result.data && result.data.next) {
                        window.location.href = result.data.next;
                        return;
                    }
                    if (requestSpec.redirectOnSuccess) {
                        window.location.href = requestSpec.redirectOnSuccess;
                        return;
                    }
                    if (result.data && result.data.next && form.getAttribute('data-api-follow-next') === '1') {
                        window.location.href = result.data.next;
                        return;
                    }
                    finishSuccess(form, result);
                })
                .catch(function (error) {
                    console.error(error);
                    alert('网络错误，请稍后重试');
                })
                .finally(function () {
                    form.dataset.apiBusy = '0';
                    setFormBusy(form, false);
                });
        }, true);
    }

    // Auto-bind once (safe if script is included multiple times)
    if (!global.__MangaDockApiFormsBound) {
        global.__MangaDockApiFormsBound = true;
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', function () {
                bindApiForms(document);
            });
        } else {
            bindApiForms(document);
        }
    }

    global.MangaDockApi = {
        apiV1: apiV1,
        getCsrfToken: getCsrfToken,
        errorMessage: errorMessage,
        formatClock: formatClock,
        bindApiForms: bindApiForms,
    };
})(window);
