"""Reversible delivery control, without database or download calls."""
import json, os, urllib.request
action = os.environ['QUEUE_CONTROL']
target = os.environ['DEPLOY_TARGET']
if action not in ('pause', 'resume') or target not in ('staging', 'production'):
    raise ValueError('Invalid queue control')
account = os.environ['CLOUDFLARE_ACCOUNT_ID']
token = os.environ['CLOUDFLARE_API_TOKEN']
def call(path, method='GET', body=None):
    request = urllib.request.Request(
        'https://api.cloudflare.com/client/v4/accounts/' + account + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    if not data.get('success'):
        raise RuntimeError('Queue control rejected')
    return data['result']
changed = []
for page in range(1, 101):
    queues = call('/queues?per_page=100&page=' + str(page))
    for queue in queues:
        name = queue['queue_name']
        if not name.startswith('mangadock-') or not name.endswith('-' + target):
            continue
        call('/queues/' + queue['queue_id'], 'PATCH',
             {'queue_name': name, 'settings': {'delivery_paused': action == 'pause'}})
        changed.append(name)
    if len(queues) < 100:
        break
if not changed:
    raise RuntimeError('No matching MangaDock queue')
print(json.dumps({'action': action, 'queues': changed}))
