"""Multi-URL and playlist batches: each selected item is a normal downloadable job.

No ZIP is generated. The same opaque batch token grants access only to its children.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import HTTPException

MAX_WORKERS = 8
GENERIC_PROFILES = [
    {'id': f'video-mp4-{h}', 'kind': 'video', 'ext': 'MP4', 'label': f'Até {h}p', 'height': h}
    for h in (1080, 720, 480, 360)
] + [
    {'id': 'audio-m4a-best', 'kind': 'audio', 'ext': 'M4A', 'label': 'Melhor disponível'},
    {'id': 'audio-mp3-192', 'kind': 'audio', 'ext': 'MP3', 'label': '192 kbps'},
    {'id': 'audio-mp3-128', 'kind': 'audio', 'ext': 'MP3', 'label': '128 kbps'},
]


def split_links(text: str) -> list[str]:
    """Commas, semicolons, whitespace (including newlines) separate whole links."""
    return list(dict.fromkeys(x for x in re.split(r'[,;\s]+', text.strip()) if x))


def inspect_collection(m: Any, text: str) -> dict[str, Any]:
    links = split_links(text)
    if not links or len(links) > m.MAX_PLAYLIST_ITEMS:
        raise m.api_error('PLAYLIST_LIMIT' if links else 'INVALID_URL', 413 if links else 400)
    normalized = [m.normalized_url(link) for link in links]
    entries: list[dict[str, Any]] = []
    failures = []
    known = set()
    def single(item):
        url, source = item
        try:
            info = m.inspect_media(url)
            return {'url': url, 'title': info['title'], 'source': source,
                    'publicEligible': info.get('publicEligible', True), 'thumbnail': info['thumbnail'],
                    'profiles': info['profiles'], 'duration': info['duration']}
        except HTTPException as exc:
            return {'url': url, 'error': exc.detail.get('message', 'Mídia indisponível') if isinstance(exc.detail, dict) else 'Mídia indisponível'}
    # Inspect individual URLs in bounded parallelism, not all at once.
    with ThreadPoolExecutor(max_workers=2) as pool:
        unique = list(dict.fromkeys(normalized))
        non_playlists = [x for x in unique if '/playlist?list=' not in x[0]]
        results = dict(zip(non_playlists, pool.map(single, non_playlists)))
        for url, source in normalized:
            if '/playlist?list=' in url:
                info = m.inspect_playlist(url)
                for video in info['entries']:
                    if video['url'] in known:
                        continue
                    known.add(video['url'])
                    entries.append({'url': video['url'], 'title': video['title'], 'source': 'YouTube',
                                    'publicEligible': False, 'thumbnail': f'https://i.ytimg.com/vi/{video["id"]}/hqdefault.jpg'})
            else:
                if url in known:
                    continue
                known.add(url)
                result = results[(url, source)]
                if 'error' in result:
                    failures.append({'url': url, 'message': result['error']})
                else:
                    entries.append(result)
            if len(entries) > m.MAX_PLAYLIST_ITEMS:
                raise m.api_error('PLAYLIST_LIMIT', 413)
    if not entries:
        raise m.api_error('PLAYLIST_EMPTY', 422)
    first = entries[0]
    profiles = {profile['id']: profile for profile in GENERIC_PROFILES}
    for entry in entries:
        for profile in entry.get('profiles', []):
            profiles[profile['id']] = profile
    return {'kind': 'batch', 'title': first['title'] if len(entries) == 1 else f'{len(entries)} vídeos',
            'author': '', 'duration': None, 'thumbnail': first.get('thumbnail', ''),
            'count': len(entries), 'entries': entries,
            'warnings': failures, 'profiles': list(profiles.values()),
            'publicEligible': all(x['publicEligible'] for x in entries), 'maxConcurrency': m.MAX_PARALLEL_DOWNLOADS}


def create_batch(m, body, request, inspection, info, profile):
    selected = body.selectedPositions
    entries = info['entries']
    if not selected or len(selected) != len(set(selected)) or any(type(p) is not int or p < 1 or p > len(entries) for p in selected):
        raise m.api_error('INVALID_SELECTION', 422)
    # Validate each explicitly chosen format against the corresponding inspected media.
    overrides = body.itemProfiles or {}
    if (len(overrides) > len(selected) or any(type(position) is not int or
            position not in selected for position in overrides)):
        raise m.api_error('INVALID_SELECTION', 422)
    allowed_profiles = {candidate['id']: candidate for candidate in info['profiles']}
    if any(profile_id not in allowed_profiles for profile_id in overrides.values()):
        raise m.api_error('INVALID_PROFILE', 422)
    for position in selected:
        available = {p['id'] for p in entries[position - 1].get('profiles', info['profiles'])}
        if overrides.get(position, profile['id']) not in available:
            raise m.api_error('INVALID_PROFILE', 422)
    if body.concurrency < 1 or body.concurrency > m.MAX_PARALLEL_DOWNLOADS:
        raise m.api_error('INVALID_CONCURRENCY', 422)
    if body.publish and not all(entries[p - 1]['publicEligible'] for p in selected):
        raise m.api_error('UNAVAILABLE', 422)
    job_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
    hashed = hashlib.sha256(token.encode()).hexdigest()
    ip = getattr(m, 'admin_client_ip', lambda _: None)(request)
    with m.db() as c:
        queued = c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running') AND parent_id IS NULL").fetchone()[0]
        if queued >= m.MAX_QUEUE:
            raise m.api_error('BUSY', 429)
        c.execute('''INSERT INTO jobs (id,token_hash,url,title,profile,metadata,publish,source,status,stage,
                      percent,created,kind,concurrency,client_ip)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (job_id, hashed, inspection['url'], info['title'], json.dumps(profile), int(body.metadata),
                   0, 'YouTube/Vimeo', 'queued', 'Na fila', 0, m.epoch(), 'batch', body.concurrency, ip))
        for pos in selected:
            item = entries[pos - 1]
            item_profile = allowed_profiles[overrides.get(pos, profile['id'])]
            c.execute('''INSERT INTO jobs (id,token_hash,url,title,profile,metadata,publish,source,status,stage,
                          percent,created,kind,parent_id,position,client_ip)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (uuid.uuid4().hex, hashed, item['url'], item['title'], json.dumps(item_profile), int(body.metadata),
                       int(body.publish), item['source'], 'queued', 'Na fila', None, m.epoch(), 'single',job_id,pos,ip))
    return {'id': job_id, 'token': token, 'status': 'queued', 'kind': 'batch'}


def batch_status(m, row):
    with m.db() as c:
        children = c.execute('''SELECT id,position,title,profile,status,stage,percent,error_code,filename,filesize,expires
                                FROM jobs WHERE parent_id=? ORDER BY position''', (row['id'],)).fetchall()
    items = []
    for x in children:
        items.append({'id': x['id'], 'position': x['position'], 'title': x['title'], 'status': x['status'],
                      'stage': x['stage'], 'profile': json.loads(x['profile']), 'percent': round(x['percent'],1) if x['percent'] is not None else None,
                      'error': {'code': x['error_code'], 'message': m.MESSAGES.get(x['error_code'], m.MESSAGES['PROCESSING'])} if x['error_code'] else None,
                      'fileName': x['filename'] if x['status'] == 'completed' else None,
                      'fileSize': x['filesize'] if x['status'] == 'completed' else None,
                      'expiresAt': x['expires'] if x['status'] == 'completed' else None})
    completed = sum(x['status']=='completed' for x in items)
    active = sum(x['status'] in ('queued','running') for x in items)
    errors = sum(x['status']=='failed' for x in items)
    done = sum(x['status'] in ('completed','failed','canceled','expired') for x in items)
    percent = round(sum((x['percent'] if x['percent'] is not None else 0) if x['status'] in ('queued','running')
                        else 100 for x in items) / max(1,len(items)),1)
    status = row['status']
    if row['status'] == 'expired' and not active:
        status = 'expired'
    elif not active:
        status = 'canceled' if row['cancel_requested'] else 'completed' if completed else 'failed'
    elif any(x['status']=='running' for x in items):
        status = 'running'
    stage = f'{done}/{len(items)} processados'
    return {'id': row['id'], 'kind':'batch', 'status': status, 'stage':stage,
            'percent': percent, 'error': None if completed or active else {'code':'PROCESSING','message':m.MESSAGES['PROCESSING']},
            'batch': {'total':len(items), 'completed':completed,'failed':errors,'items':items,
                      'concurrency':row['concurrency']}}


def refresh_parent(m, parent_id):
    with m.db() as c:
        rows = c.execute('SELECT status,expires FROM jobs WHERE parent_id=?', (parent_id,)).fetchall()
        if not rows or any(r['status'] in ('queued','running') for r in rows):
            return
        parent = c.execute('SELECT cancel_requested FROM jobs WHERE id=?', (parent_id,)).fetchone()
        if not parent:
            return
        status = 'canceled' if parent['cancel_requested'] else 'completed' if any(r['status']=='completed' for r in rows) else 'failed'
        expires = max((r['expires'] or 0 for r in rows), default=0) or None
        c.execute('UPDATE jobs SET status=?,stage=?,percent=100,completed=?,expires=? WHERE id=?',
                  (status, 'Concluído' if status=='completed' else 'Cancelado' if status=='canceled' else 'Falha', m.epoch(),expires,parent_id))


def cancel_batch(m, row):
    jid = row['id']
    with m.db() as c:
        c.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (jid,))
        c.execute('''UPDATE jobs SET status='canceled',stage='Cancelado',error_code='CANCELED',cancel_requested=1
                     WHERE parent_id=? AND status='queued' ''', (jid,))
        c.execute('''UPDATE jobs SET cancel_requested=1 WHERE parent_id=? AND status='running' ''', (jid,))
        proc_ids = [r['id'] for r in c.execute("SELECT id FROM jobs WHERE parent_id=? AND status='running'",(jid,))]
    import os,signal
    with m.lock:
        for id in proc_ids:
            proc = m.processes.get(id)
            if proc and proc.poll() is None:
                try: os.killpg(proc.pid,signal.SIGTERM)
                except ProcessLookupError: pass
    refresh_parent(m,jid)
    return {'status':'canceling'}


def claim_next(m, c):
    global_running = c.execute("SELECT count(*) FROM jobs WHERE status='running' AND kind!='batch'").fetchone()[0]
    if global_running >= m.MAX_PARALLEL_DOWNLOADS:
        return None
    jobs = c.execute("""SELECT j.* FROM jobs j
                 LEFT JOIN jobs p ON j.parent_id=p.id
                 WHERE j.status='queued' AND j.cancel_requested=0 AND j.kind!='batch'
                 AND (j.parent_id IS NULL OR (p.status IN ('queued','running') AND p.cancel_requested=0
                 AND (SELECT count(*) FROM jobs z WHERE z.parent_id=j.parent_id AND z.status='running') < p.concurrency))
                 ORDER BY j.created,j.position,j.id LIMIT 1""").fetchone()
    if jobs:
        c.execute("UPDATE jobs SET status='running' WHERE id=? AND status='queued'", (jobs['id'],))
        if jobs['parent_id']:
            c.execute("UPDATE jobs SET status='running' WHERE id=? AND status='queued'", (jobs['parent_id'],))
    return jobs