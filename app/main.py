"""API, SQLite and single-worker media queue for a self-hosted public downloader."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DATA = Path(os.getenv('DATA_DIR', '/data')).resolve()
WORK = DATA / 'jobs'
WEB = Path(__file__).resolve().parents[1] / 'web'
DB_PATH = DATA / 'app.sqlite3'
MAX_FILE_MB = max(1, int(os.getenv('MAX_FILE_MB', '1024')))
MAX_BYTES = MAX_FILE_MB * 1024 * 1024
# Encoding and muxing can temporarily retain both streams and the final file.
# Do not compare the sum of intermediate files against the final output limit.
MAX_TEMP_MB = max(MAX_FILE_MB, int(os.getenv('MAX_TEMP_MB', str(max(4096, 4 * MAX_FILE_MB)))))
MAX_TEMP_BYTES = MAX_TEMP_MB * 1024 * 1024
MAX_DURATION = int(os.getenv('MAX_DURATION_SECONDS', '5400'))
EXPIRES_SECONDS = int(os.getenv('FILE_TTL_SECONDS', '7200'))
INSPECT_TTL = 1800
INSPECT_TIMEOUT = 45
JOB_TIMEOUT = int(os.getenv('JOB_TIMEOUT_SECONDS', '2700'))
MAX_QUEUE = 10
MAX_PLAYLIST_ITEMS = max(1, min(50, int(os.getenv('MAX_PLAYLIST_ITEMS', '20'))))
MAX_PLAYLIST_TOTAL_MB = max(1, int(os.getenv('MAX_PLAYLIST_TOTAL_MB', '2048')))
MAX_PLAYLIST_TOTAL_BYTES = MAX_PLAYLIST_TOTAL_MB * 1024 * 1024
# ZIP packaging temporarily duplicates the retained videos; allow working space.
MAX_PLAYLIST_TEMP_MB = max(MAX_PLAYLIST_TOTAL_MB * 2, int(os.getenv('MAX_PLAYLIST_TEMP_MB', str(max(6144, 3 * MAX_PLAYLIST_TOTAL_MB)))))
MAX_PLAYLIST_TEMP_BYTES = MAX_PLAYLIST_TEMP_MB * 1024 * 1024
PLAYLIST_INSPECT_TIMEOUT = 90
MAX_INSPECT_CONCURRENT = 2
HOST = os.getenv('PUBLIC_HOST', 'yt.ulln.com.br')
IP_RETENTION_DAYS = max(1,min(30,int(os.getenv('IP_RETENTION_DAYS','7'))))
JOB_RETENTION_DAYS = max(7,min(365,int(os.getenv('JOB_RETENTION_DAYS','30'))))
MAX_PARALLEL_DOWNLOADS = max(1,min(8,int(os.getenv('MAX_PARALLEL_DOWNLOADS','2'))))
APP_VERSION = '0.4.0'

DATA.mkdir(parents=True, exist_ok=True)
WORK.mkdir(parents=True, exist_ok=True)
lock = threading.RLock()
inspect_slots = threading.BoundedSemaphore(MAX_INSPECT_CONCURRENT)
runner_started = False
processes: dict[str, subprocess.Popen] = {}
rate: dict[str, deque[float]] = defaultdict(deque)
app = FastAPI(title='yt-downloader', version=APP_VERSION, docs_url=None, redoc_url=None, openapi_url=None)


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch() -> int:
    return int(time.time())


@contextmanager
def db():
    with lock:
        conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS inspections (
          id TEXT PRIMARY KEY, url TEXT NOT NULL, metadata TEXT NOT NULL,
          created INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, url TEXT NOT NULL,
          title TEXT NOT NULL, profile TEXT NOT NULL, metadata INTEGER NOT NULL,
          publish INTEGER NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
          stage TEXT NOT NULL, percent REAL, error_code TEXT, filename TEXT,
          filesize INTEGER, created INTEGER NOT NULL, completed INTEGER,
          expires INTEGER, cancel_requested INTEGER DEFAULT 0,
          kind TEXT NOT NULL DEFAULT 'single'
        );
        CREATE TABLE IF NOT EXISTS public_history (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
          source TEXT NOT NULL, profile TEXT NOT NULL, created INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS playlist_items (
          job_id TEXT NOT NULL, position INTEGER NOT NULL, video_id TEXT NOT NULL,
          title TEXT NOT NULL, url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          stage TEXT NOT NULL DEFAULT 'Na fila', percent REAL,
          error_code TEXT, filename TEXT, filesize INTEGER,
          PRIMARY KEY(job_id, position)
        );
        CREATE INDEX IF NOT EXISTS idx_playlist_items_job ON playlist_items(job_id,position);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status,created);
        CREATE INDEX IF NOT EXISTS idx_history_created ON public_history(created);
        ''')
        # Preserve existing installations created before playlist support.
        columns = {row['name'] for row in c.execute('PRAGMA table_info(jobs)')}
        if 'kind' not in columns:
            c.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'single'")
        if 'parent_id' not in columns:
            c.execute('ALTER TABLE jobs ADD COLUMN parent_id TEXT')
        if 'position' not in columns:
            c.execute('ALTER TABLE jobs ADD COLUMN position INTEGER')
        if 'concurrency' not in columns:
            c.execute('ALTER TABLE jobs ADD COLUMN concurrency INTEGER DEFAULT 1')
        c.execute('CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_id,position)')
        # A restarted download cannot be resumed mid-FFmpeg without a fresh job.
        c.execute("UPDATE jobs SET status='failed', stage='Falha', error_code='INTERRUPTED' WHERE status='running' AND kind!='batch'")
        # After restart, still-queued batch children continue; an in-flight child cannot resume.
        c.execute("""UPDATE jobs SET status='queued', stage='Na fila' WHERE kind='batch'
                     AND status='running' AND EXISTS
                     (SELECT 1 FROM jobs child WHERE child.parent_id=jobs.id AND child.status='queued')""")
        # No new ZIP archives are created; retire pending jobs using the old engine.
        c.execute("UPDATE jobs SET status='failed',stage='Falha',error_code='INTERRUPTED' WHERE kind='playlist' AND status='queued'")
    admin_schema()


class InspectBody(BaseModel):
    url: str = Field(min_length=1, max_length=12000)


class DownloadBody(BaseModel):
    inspectionId: str
    profileId: str
    metadata: bool = True
    publish: bool = False
    selectedPositions: list[int] | None = None
    concurrency: int = Field(default=1, ge=1, le=8)


MESSAGES = {
    'INVALID_URL': 'Confira o endereço do vídeo e tente novamente.',
    'UNSUPPORTED': 'Use um link do YouTube ou Vimeo.',
    'NOT_FOUND': 'Não conseguimos encontrar esse vídeo.',
    'PRIVATE': 'Não foi possível acessar este vídeo. Verifique as permissões.',
    'UNAVAILABLE': 'Este vídeo não está disponível para download.',
    'FORMATS': 'Nenhum formato compatível foi encontrado.',
    'TOO_LONG': 'O vídeo excede a duração permitida.',
    'TOO_LARGE': f'O arquivo excedeu o limite de {MAX_FILE_MB} MB. Escolha uma qualidade menor.',
    'TEMP_LIMIT': 'O processamento excedeu o espaço temporário configurado no servidor.',
    'PLAYLIST_TOTAL_LIMIT': f'A playlist excedeu o limite total de {MAX_PLAYLIST_TOTAL_MB} MB.',
    'NO_SPACE': 'O servidor está sem espaço disponível para preparar este arquivo.',
    'TIMED_OUT': 'A operação demorou demais. Tente novamente.',
    'BUSY': 'Há muitas solicitações agora. Tente novamente em instantes.',
    'INSPECTION_EXPIRED': 'Esta análise expirou. Analise o link novamente.',
    'INVALID_PROFILE': 'Esta qualidade não está disponível. Analise o link novamente.',
    'PROCESSING': 'Não foi possível preparar o arquivo. Tente outro formato.',
    'CANCELED': 'Processamento cancelado.',
    'INTERRUPTED': 'O servidor reiniciou durante o processamento. Tente novamente.',
    'EXPIRED': 'Este arquivo expirou. Prepare-o novamente.',
    'PLAYLIST_LIMIT': 'A playlist ultrapassa o limite de vídeos. Use uma lista menor.',
    'PLAYLIST_EMPTY': 'Não encontramos vídeos públicos nesta playlist.',
    'PLAYLIST_FAILED': 'Nenhum vídeo da playlist pôde ser preparado.',
    'INVALID_SELECTION': 'Selecione pelo menos um vídeo válido da lista.',
    'INVALID_CONCURRENCY': 'Reduza a quantidade de downloads simultâneos.',
    'ADMIN_DISABLED': 'A administração não está configurada.',
    'UNAUTHORIZED': 'Acesso não autorizado.',
}


def api_error(code: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={'code': code, 'message': MESSAGES.get(code, MESSAGES['PROCESSING'])})


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and 'code' in exc.detail:
        return JSONResponse(exc.detail, status_code=exc.status_code, headers={'Cache-Control': 'no-store'})
    return JSONResponse({'code': 'NOT_FOUND', 'message': 'Endereço não encontrado.'}, status_code=exc.status_code)


@app.middleware('http')
async def response_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else 'public, max-age=300'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    return response


def normalized_url(value: str) -> tuple[str, str]:
    try:
        u = urlparse(value.strip())
        if u.scheme != 'https' or u.username or u.password or u.port or u.fragment:
            raise ValueError
        host = (u.hostname or '').lower().rstrip('.')
        if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            if u.path == '/playlist':
                playlist_id = parse_qs(u.query).get('list', [''])[0]
                if not re.fullmatch(r'[A-Za-z0-9_-]{10,100}', playlist_id):
                    raise ValueError
                return f'https://www.youtube.com/playlist?list={playlist_id}', 'YouTube'
            if u.path == '/watch':
                video_id = parse_qs(u.query).get('v', [''])[0]
            else:
                match = re.fullmatch(r'/(?:shorts|live|embed)/([\w-]{11})/?', u.path)
                video_id = match.group(1) if match else ''
            if not re.fullmatch(r'[\w-]{11}', video_id):
                raise ValueError
            return f'https://www.youtube.com/watch?v={video_id}', 'YouTube'
        if host in ('youtu.be', 'www.youtu.be'):
            match = re.fullmatch(r'/([\w-]{11})/?', u.path)
            if not match:
                raise ValueError
            return f'https://www.youtube.com/watch?v={match.group(1)}', 'YouTube'
        if host in ('vimeo.com', 'www.vimeo.com', 'player.vimeo.com'):
            match = re.fullmatch(r'/(?:video/)?(\d{1,15})/?', u.path)
            if not match:
                raise ValueError
            return f'https://vimeo.com/{match.group(1)}', 'Vimeo'
        raise api_error('UNSUPPORTED')
    except HTTPException:
        raise
    except (ValueError, TypeError):
        raise api_error('INVALID_URL')


def profiles_from(info: dict[str, Any]) -> list[dict[str, Any]]:
    formats = info.get('formats') or []
    heights: set[int] = set()
    has_audio = False
    for f in formats:
        if not isinstance(f, dict):
            continue
        if f.get('acodec') not in (None, 'none'):
            has_audio = True
        h = f.get('height')
        if f.get('vcodec') not in (None, 'none') and isinstance(h, (float, int)) and 144 <= int(h) <= 2160:
            heights.add(int(h))
    candidates = [2160, 1440, 1080, 720, 480, 360, 240, 144]
    result = []
    for h in candidates:
        if h in heights:
            result.append({'id': f'video-mp4-{h}', 'kind': 'video', 'ext': 'MP4', 'label': f'{h}p', 'height': h})
    if has_audio:
        result += [
            {'id': 'audio-m4a-best', 'kind': 'audio', 'ext': 'M4A', 'label': 'Melhor disponível'},
            {'id': 'audio-mp3-192', 'kind': 'audio', 'ext': 'MP3', 'label': '192 kbps'},
            {'id': 'audio-mp3-128', 'kind': 'audio', 'ext': 'MP3', 'label': '128 kbps'},
        ]
    return result


def failure_code(stderr: str) -> str:
    s = stderr.lower()
    if any(v in s for v in ('no space left on device', 'errno 28', 'disk full')):
        return 'NO_SPACE'
    if any(v in s for v in ('max-filesize', 'larger than max filesize', 'file is larger than', 'file size exceeds')):
        return 'TOO_LARGE'
    if any(v in s for v in ('private video', 'private video.', 'permission', 'login required', 'sign in')):
        return 'PRIVATE'
    if any(v in s for v in ('file is larger than', 'file is too large', 'exceeds max', 'max-filesize')):
        return 'TOO_LARGE'
    if any(v in s for v in ('not available', 'unavailable', 'copyright', 'geo-restricted', 'forbidden', 'unsupported url')):
        return 'UNAVAILABLE'
    if any(v in s for v in ('not found', '404', 'does not exist', 'video has been removed')):
        return 'NOT_FOUND'
    if any(v in s for v in ('requested format is not available', 'no video formats found')):
        return 'FORMATS'
    return 'PROCESSING'


def inspect_media(url: str) -> dict[str, Any]:
    command = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
               '--skip-download', '--dump-single-json', '--no-warnings', '--js-runtimes', 'node',
               '--socket-timeout', '12', '--retries', '1', url]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=INSPECT_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise api_error('TIMED_OUT', 504)
    if done.returncode != 0:
        raise api_error(failure_code(done.stderr), 422)
    try:
        info = json.loads(done.stdout)
    except (ValueError, TypeError):
        raise api_error('UNAVAILABLE', 422)
    if info.get('_type') in ('playlist', 'multi_video') or info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming'):
        raise api_error('UNAVAILABLE', 422)
    duration = info.get('duration')
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise api_error('UNAVAILABLE', 422)
    if duration > MAX_DURATION:
        raise api_error('TOO_LONG', 413)
    profiles = profiles_from(info)
    if not profiles:
        raise api_error('FORMATS', 422)
    thumbnail = str(info.get('thumbnail') or '')
    if not thumbnail.startswith('https://'):
        thumbnail = ''
    public_eligible = str(info.get('availability') or 'public').lower() not in ('unlisted', 'private', 'needs_auth', 'premium_only')
    return {'publicEligible': public_eligible, 'title': str(info.get('title') or 'Vídeo')[:300],
            'author': str(info.get('uploader') or info.get('channel') or '')[:200],
            'duration': int(duration), 'thumbnail': thumbnail,
            'profiles': profiles}


def inspect_playlist(url: str) -> dict[str, Any]:
    """Fetch only bounded flat playlist entries; no media is downloaded here."""
    command = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--flat-playlist',
               '--playlist-end', str(MAX_PLAYLIST_ITEMS + 1), '--skip-download',
               '--dump-single-json', '--no-warnings', '--js-runtimes', 'node',
               '--socket-timeout', '12', '--retries', '1', url]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=PLAYLIST_INSPECT_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise api_error('TIMED_OUT', 504)
    if done.returncode != 0:
        raise api_error(failure_code(done.stderr), 422)
    try:
        info = json.loads(done.stdout)
    except (ValueError, TypeError):
        raise api_error('UNAVAILABLE', 422)
    if info.get('_type') != 'playlist':
        raise api_error('UNAVAILABLE', 422)
    entries = info.get('entries') or []
    if len(entries) > MAX_PLAYLIST_ITEMS:
        raise api_error('PLAYLIST_LIMIT', 413)
    videos = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = str(entry.get('id') or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            continue
        videos.append({'id': video_id, 'title': str(entry.get('title') or f'Vídeo {len(videos)+1}')[:300],
                       'url': f'https://www.youtube.com/watch?v={video_id}'})
    if not videos:
        raise api_error('PLAYLIST_EMPTY', 422)
    profiles = [
        {'id': f'video-mp4-{height}', 'kind': 'video', 'ext': 'MP4',
         'label': f'Até {height}p', 'height': height}
        for height in (1080, 720, 480)
    ] + [
        {'id': 'audio-m4a-best', 'kind': 'audio', 'ext': 'M4A', 'label': 'Melhor disponível'},
        {'id': 'audio-mp3-192', 'kind': 'audio', 'ext': 'MP3', 'label': '192 kbps'},
        {'id': 'audio-mp3-128', 'kind': 'audio', 'ext': 'MP3', 'label': '128 kbps'},
    ]
    return {'kind': 'playlist', 'title': str(info.get('title') or 'Playlist')[:300],
            'author': str(info.get('uploader') or info.get('channel') or '')[:200],
            'duration': None, 'thumbnail': f'https://i.ytimg.com/vi/{videos[0]["id"]}/hqdefault.jpg',
            'profiles': profiles, 'entries': videos, 'count': len(videos),
            # Flat listing does not prove that all entries are publicly listed.
            'publicEligible': False}


def client_key(request: Request) -> str:
    # An anonymous ephemeral key; never persisted, never shown in history.
    remote = request.client.host if request.client else 'unknown'
    # Trust the forwarded address only when the tunnel is the sole entrypoint.
    # A published Docker port may report the Docker gateway as request.client.host.
    if os.getenv('TRUST_CF_IP') == '1':
        candidate = request.headers.get('cf-connecting-ip', '')
        try:
            remote = str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return hashlib.sha256(remote.encode()).hexdigest()


def allow_rate(request: Request, maximum: int, window: int) -> None:
    with lock:
        key = f'{client_key(request)}:{request.url.path}'
        q = rate[key]
        now = time.monotonic()
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= maximum:
            raise api_error('BUSY', 429)
        q.append(now)
        if len(rate) > 5000:
            rate.clear()


@app.get('/api/health')
def health():
    return {'status': 'ok', 'version': APP_VERSION}


@app.post('/api/inspect')
def inspect(body: InspectBody, request: Request):
    allow_rate(request, 12, 600)
    links = split_links(body.url)
    if len(links) == 1:
        url, source = normalized_url(links[0])
    else:
        url, source = '', 'YouTube/Vimeo'
    if not inspect_slots.acquire(blocking=False):
        raise api_error('BUSY', 429)
    try:
        info = inspect_collection(sys.modules[__name__], body.url) if len(links)>1 or '/playlist?list=' in url else inspect_media(url)
    finally:
        inspect_slots.release()
    inspection_id = uuid.uuid4().hex
    with db() as c:
        c.execute('INSERT INTO inspections VALUES(?,?,?,?)', (inspection_id, url or body.url.strip(), json.dumps({'source': source, **info}), epoch()))
    return {'inspectionId': inspection_id, 'source': source, **info}


def public_job(row: sqlite3.Row) -> dict[str, Any]:
    if row['kind'] == 'batch':
        return batch_status(sys.modules[__name__], row)
    result = {'id': row['id'], 'status': row['status'], 'stage': row['stage'],
              'percent': round(row['percent'], 1) if row['percent'] is not None else None,
              'error': {'code': row['error_code'], 'message': MESSAGES.get(row['error_code'], MESSAGES['PROCESSING'])} if row['error_code'] else None,
              'fileName': row['filename'] if row['status'] == 'completed' else None,
              'fileSize': row['filesize'] if row['status'] == 'completed' else None,
              'expiresAt': row['expires'] if row['status'] == 'completed' else None,
              'kind': row['kind']}
    if row['kind'] == 'playlist':
        with db() as c:
            items = c.execute('''SELECT position,title,status,stage,percent,error_code
                                 FROM playlist_items WHERE job_id=? ORDER BY position''', (row['id'],)).fetchall()
        result['playlist'] = {'total': len(items),
                              'completed': sum(x['status'] == 'completed' for x in items),
                              'failed': sum(x['status'] == 'failed' for x in items),
                              'items': [{'position': x['position'], 'title': x['title'],
                                         'status': x['status'], 'stage': x['stage'],
                                         'percent': round(x['percent'], 1) if x['percent'] is not None else None,
                                         'error': MESSAGES.get(x['error_code'], MESSAGES['PROCESSING']) if x['error_code'] else None}
                                        for x in items]}
    return result


def require_job(job_id: str, token: str) -> sqlite3.Row:
    with db() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row or not token or not hmac.compare_digest(row['token_hash'], hashlib.sha256(token.encode()).hexdigest()):
        raise HTTPException(404)
    return row


@app.post('/api/downloads', status_code=202)
def download(body: DownloadBody, request: Request):
    allow_rate(request, 6, 600)
    with db() as c:
        inspection = c.execute('SELECT * FROM inspections WHERE id=?', (body.inspectionId,)).fetchone()
        if not inspection or epoch() - inspection['created'] > INSPECT_TTL:
            raise api_error('INSPECTION_EXPIRED', 410)
        info = json.loads(inspection['metadata'])
        profile = next((p for p in info['profiles'] if p['id'] == body.profileId), None)
        if not profile:
            raise api_error('INVALID_PROFILE', 422)
        pending = c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running') AND parent_id IS NULL").fetchone()[0]
        if pending >= MAX_QUEUE:
            raise api_error('BUSY', 429)
        if info.get('kind') == 'batch':
            return create_batch(sys.modules[__name__], body, request, inspection, info, profile)
        if body.publish and not info.get('publicEligible', True):
            raise api_error('UNAVAILABLE', 422)
        job_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        kind = info.get('kind', 'single')
        c.execute('''INSERT INTO jobs
          (id,token_hash,url,title,profile,metadata,publish,source,status,stage,percent,created,kind)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (job_id, hashlib.sha256(token.encode()).hexdigest(), inspection['url'], info['title'],
           json.dumps(profile), int(body.metadata), int(body.publish), info['source'],
           'queued', 'Na fila', None, epoch(), kind))
        c.execute('UPDATE jobs SET client_ip=? WHERE id=?', (admin_client_ip(request), job_id))
        if kind == 'playlist':
            for position, entry in enumerate(info['entries'], 1):
                c.execute('''INSERT INTO playlist_items(job_id,position,video_id,title,url)
                             VALUES(?,?,?,?,?)''', (job_id, position, entry['id'], entry['title'], entry['url']))
    return {'id': job_id, 'token': token, 'status': 'queued'}


@app.get('/api/downloads/{job_id}')
def status(job_id: str, token: str = ''):
    return public_job(require_job(job_id, token))


@app.get('/api/downloads/{job_id}/events')
async def events(job_id: str, token: str = ''):
    require_job(job_id, token)
    async def stream():
        last = None
        for _ in range(4800):  # maximum open SSE lifetime ~40 minutes
            row = require_job(job_id, token)
            state = public_job(row)
            serialized = json.dumps(state, ensure_ascii=False)
            if serialized != last:
                yield f'data: {serialized}\n\n'
                last = serialized
            else:
                yield ': ping\n\n'
            if state['status'] in ('completed', 'failed', 'canceled', 'expired'):
                return
            await asyncio.sleep(.5)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


@app.post('/api/downloads/{job_id}/cancel')
def cancel(job_id: str, token: str = ''):
    row = require_job(job_id, token)
    if row['status'] in ('completed', 'failed', 'expired', 'canceled'):
        return public_job(row)
    if row['kind'] == 'batch':
        return cancel_batch(sys.modules[__name__], row)
    with db() as c:
        c.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (job_id,))
        if row['status'] == 'queued':
            c.execute("UPDATE jobs SET status='canceled', stage='Cancelado', error_code='CANCELED' WHERE id=?", (job_id,))
    with lock:
        proc = processes.get(job_id)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    return {'status': 'canceling'}


def safe_filename(title: str, ext: str) -> str:
    cleaned = re.sub(r'[^\w\- .()]', '', title, flags=re.UNICODE).strip(' .')[:110]
    return f'{cleaned or "arquivo"}.{ext.lower()}'


@app.get('/api/downloads/{job_id}/file')
def file(job_id: str, token: str = ''):
    row = require_job(job_id, token)
    if row['kind'] == 'batch' or row['status'] != 'completed' or not row['expires'] or epoch() > row['expires']:
        raise api_error('EXPIRED', 410)
    try:
        allowed = ('.zip',) if row['kind'] == 'playlist' else ('.mp4', '.mp3', '.m4a')
        paths = [p for p in (WORK / job_id).iterdir() if p.is_file() and p.name.startswith('media.') and p.suffix.lower() in allowed]
    except FileNotFoundError:
        raise api_error('EXPIRED', 410)
    if not paths:
        raise api_error('EXPIRED', 410)
    mime = {'.mp4': 'video/mp4', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.zip': 'application/zip'}[paths[0].suffix.lower()]
    response = FileResponse(paths[0], media_type=mime, filename=row['filename'])
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@app.get('/api/history')
def history(limit: int = 30, offset: int = 0):
    limit = min(max(1, limit), 50)
    offset = min(max(0, offset), 10000)
    with db() as c:
        rows = c.execute('SELECT title,url,source,profile,created FROM public_history ORDER BY created DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
    return {'items': [{'title': r['title'], 'url': r['url'], 'source': r['source'],
                       'profile': json.loads(r['profile']), 'createdAt': r['created']} for r in rows]}


def set_job(job_id: str, **fields):
    if not fields:
        return
    with db() as c:
        c.execute('UPDATE jobs SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=?', (*fields.values(), job_id))


def media_command(url: str, profile: dict[str, Any], folder: Path, embed: bool) -> list[str]:
    kind, ext = profile['kind'], profile['ext'].lower()
    command = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--no-warnings',
               '--newline', '--js-runtimes', 'node', '--socket-timeout', '15', '--retries', '2', '--fragment-retries', '2',
               '--max-filesize', str(MAX_BYTES), '--progress-template',
               'download:DL:%(progress._percent_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s',
               '--output', str(folder / 'media.%(ext)s')]
    if kind == 'video':
        h = int(profile['height'])
        command += ['--format', f'bv*[height<={h}]+ba/b[height<={h}]', '--merge-output-format', 'mp4', '--recode-video', 'mp4']
    else:
        command += ['--format', 'bestaudio/best', '--extract-audio', '--audio-format', ext]
        if ext == 'mp3':
            command += ['--audio-quality', profile['label'].split()[0] + 'K']
    if embed:
        command += ['--embed-metadata', '--embed-thumbnail']
    command += [url]
    return command


def execute_job(row: sqlite3.Row) -> None:
    job_id = row['id']
    folder = WORK / job_id
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile = json.loads(row['profile'])
    kind, ext = profile['kind'], profile['ext'].lower()
    command = media_command(row['url'], profile, folder, bool(row['metadata']))
    set_job(job_id, status='running', stage='Obtendo arquivo', percent=None)
    proc = None
    deadline = time.monotonic() + JOB_TIMEOUT
    last_output = ''
    stop_reason: list[str] = []
    try:
        proc = subprocess.Popen(command, cwd=str(folder), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors='replace', bufsize=1, start_new_session=True)
        with lock:
            processes[job_id] = proc
        assert proc.stdout is not None
        # The parent runs a watchdog so cancellation/timeout applies while output is quiet.
        def watchdog():
            while proc.poll() is None:
                with db() as c:
                    state = c.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()
                reason = ('CANCELED' if state and state['cancel_requested'] else
                          'TIMED_OUT' if time.monotonic() >= deadline else
                          'TEMP_LIMIT' if folder_size(folder) >= MAX_TEMP_BYTES else None)
                if reason:
                    stop_reason.append(reason)
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    return
                time.sleep(1)
        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()
        for line in proc.stdout:
            line = line.strip()
            last_output = (last_output + '\n' + line)[-7000:]
            if 'DL:' in line:
                match = re.search(r'DL:\s*([\d.,]+)%', line)
                pct = float(match.group(1).replace(',', '.')) if match else None
                if pct is not None:
                    set_job(job_id, stage='Obtendo arquivo', percent=min(99, max(0, pct)))
            elif any(s in line for s in ('[Merger]', '[VideoConvertor]', '[ExtractAudio]', '[Fixup')):
                set_job(job_id, stage='Convertendo arquivo', percent=None)
            elif any(s in line for s in ('[Metadata]', '[EmbedThumbnail]')):
                set_job(job_id, stage='Adicionando metadados', percent=None)
        rc = proc.wait()
        with db() as c:
            canceled = c.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()['cancel_requested']
        if canceled:
            raise RuntimeError('CANCELED')
        if stop_reason:
            raise RuntimeError(stop_reason[0])
        if time.monotonic() >= deadline:
            raise RuntimeError('TIMED_OUT')
        if folder_size(folder) >= MAX_TEMP_BYTES:
            raise RuntimeError('TEMP_LIMIT')
        if rc != 0:
            raise RuntimeError(failure_code(last_output))
        results = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == '.' + ext]
        if not results:
            raise RuntimeError('PROCESSING')
        output = max(results, key=lambda p: p.stat().st_size)
        final = folder / f'media.{ext}'
        if output != final:
            output.rename(final)
        size = final.stat().st_size
        if size > MAX_BYTES:
            raise RuntimeError('TOO_LARGE')
        # Remove source streams/recode scratch so only the completed file remains.
        for temporary in folder.iterdir():
            if temporary.is_file() and temporary != final:
                temporary.unlink(missing_ok=True)
        filename = safe_filename(row['title'], ext)
        finished = epoch()
        with db() as c:
            c.execute('''UPDATE jobs SET status='completed',stage='Concluído',percent=100,
                         filename=?,filesize=?,completed=?,expires=? WHERE id=?''',
                      (filename,size,finished,finished+EXPIRES_SECONDS,job_id))
            if row['publish']:
                c.execute('INSERT INTO public_history VALUES(?,?,?,?,?,?)',
                          (uuid.uuid4().hex,row['title'],row['url'],row['source'],row['profile'],finished))
    except Exception as exc:
        code = str(exc) if str(exc) in MESSAGES else 'PROCESSING'
        set_job(job_id, status='canceled' if code == 'CANCELED' else 'failed',
                stage='Cancelado' if code == 'CANCELED' else 'Falha', percent=None,error_code=code)
        shutil.rmtree(folder, ignore_errors=True)
    finally:
        with lock:
            processes.pop(job_id, None)
        if row['parent_id']:
            refresh_parent(sys.modules[__name__], row['parent_id'])


def folder_size(folder: Path) -> int:
    try:
        return sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
    except FileNotFoundError:
        return 0


def set_playlist_item(job_id: str, position: int, **fields):
    if not fields:
        return
    with db() as c:
        c.execute('UPDATE playlist_items SET ' + ','.join(f'{key}=?' for key in fields) +
                  ' WHERE job_id=? AND position=?', (*fields.values(), job_id, position))


def playlist_cancelled(job_id: str) -> bool:
    with db() as c:
        row = c.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()
    return bool(row and row['cancel_requested'])


def execute_playlist_job(row: sqlite3.Row) -> None:
    """Process entries in source order. One subprocess at a time, one ZIP at end."""
    job_id = row['id']
    folder = WORK / job_id
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile = json.loads(row['profile'])
    ext = profile['ext'].lower()
    with db() as c:
        entries = c.execute('SELECT * FROM playlist_items WHERE job_id=? ORDER BY position', (job_id,)).fetchall()
    total = len(entries)
    successes: list[tuple[int, Path]] = []
    try:
        for item in entries:
            position = item['position']
            if playlist_cancelled(job_id):
                raise RuntimeError('CANCELED')
            # Stop if aggregate storage has reached the safe limit.
            accumulated = sum(path.stat().st_size for _, path in successes)
            if accumulated >= MAX_PLAYLIST_TOTAL_BYTES:
                for pending in entries[position-1:]:
                    set_playlist_item(job_id, pending['position'], status='failed', stage='Falha', error_code='PLAYLIST_TOTAL_LIMIT')
                break
            item_folder = folder / f'work-{position:03d}'
            item_folder.mkdir(mode=0o700, exist_ok=True)
            stage = f'Obtendo {position}/{total}'
            set_job(job_id, status='running', stage=stage, percent=100 * (position - 1) / total)
            set_playlist_item(job_id, position, status='running', stage='Obtendo arquivo', percent=None)
            command = media_command(item['url'], profile, item_folder, bool(row['metadata']))
            command[command.index('--max-filesize'):command.index('--max-filesize')+2] = ['--max-filesize', str(min(MAX_BYTES, MAX_PLAYLIST_TOTAL_BYTES))]
            command[command.index('--output'):command.index('--output')] = [
                '--match-filter', f'duration <= {MAX_DURATION} & !is_live']
            deadline = time.monotonic() + JOB_TIMEOUT
            code = None
            tail = ''
            proc = None
            stop_reason: list[str] = []
            try:
                proc = subprocess.Popen(command, cwd=str(item_folder), stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, errors='replace',
                                        bufsize=1, start_new_session=True)
                with lock:
                    processes[job_id] = proc
                def watchdog():
                    while proc.poll() is None:
                        reason = ('CANCELED' if playlist_cancelled(job_id) else
                                  'TIMED_OUT' if time.monotonic() >= deadline else
                                  'TEMP_LIMIT' if folder_tree_size(folder) >= MAX_PLAYLIST_TEMP_BYTES or
                                                  folder_tree_size(item_folder) >= MAX_TEMP_BYTES else None)
                        if reason:
                            stop_reason.append(reason)
                            try:
                                os.killpg(proc.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                            return
                        time.sleep(1)
                threading.Thread(target=watchdog, daemon=True).start()
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.strip()
                    tail = (tail + '\n' + line)[-7000:]
                    if 'DL:' in line:
                        found = re.search(r'DL:\s*([\d.,]+)%', line)
                        pct = float(found.group(1).replace(',', '.')) if found else None
                        if pct is not None:
                            pct = min(99, max(0, pct))
                            set_playlist_item(job_id, position, stage='Obtendo arquivo', percent=pct)
                            set_job(job_id, percent=round(((position - 1) + pct / 100) / total * 100, 2))
                    elif any(x in line for x in ('[Merger]', '[VideoConvertor]', '[ExtractAudio]', '[Fixup')):
                        set_playlist_item(job_id, position, stage='Convertendo arquivo', percent=None)
                        set_job(job_id, stage=f'Convertendo {position}/{total}', percent=None)
                    elif any(x in line for x in ('[Metadata]', '[EmbedThumbnail]')):
                        set_playlist_item(job_id, position, stage='Adicionando metadados', percent=None)
                        set_job(job_id, stage=f'Metadados {position}/{total}', percent=None)
                rc = proc.wait()
                if playlist_cancelled(job_id):
                    raise RuntimeError('CANCELED')
                if stop_reason:
                    raise RuntimeError(stop_reason[0])
                if time.monotonic() >= deadline:
                    raise RuntimeError('TIMED_OUT')
                if folder_tree_size(folder) >= MAX_PLAYLIST_TEMP_BYTES or folder_tree_size(item_folder) >= MAX_TEMP_BYTES:
                    raise RuntimeError('TEMP_LIMIT')
                if rc != 0:
                    raise RuntimeError(failure_code(tail))
                output = [f for f in item_folder.iterdir() if f.is_file() and f.suffix.lower() == '.' + ext]
                if not output:
                    raise RuntimeError('PROCESSING')
                selected = max(output, key=lambda f: f.stat().st_size)
                if selected.stat().st_size > MAX_BYTES:
                    raise RuntimeError('TOO_LARGE')
                if accumulated + selected.stat().st_size > MAX_PLAYLIST_TOTAL_BYTES:
                    raise RuntimeError('PLAYLIST_TOTAL_LIMIT')
                name = f'{position:02d} - {safe_filename(item["title"], ext)}'
                target = folder / name
                selected.replace(target)
                successes.append((position, target))
                set_playlist_item(job_id, position, status='completed', stage='Concluído',
                                  percent=100, filename=name, filesize=target.stat().st_size)
            except Exception as exc:
                code = str(exc) if str(exc) in MESSAGES else 'PROCESSING'
                if code == 'CANCELED':
                    set_playlist_item(job_id, position, status='canceled', stage='Cancelado', error_code=code)
                    raise
                set_playlist_item(job_id, position, status='failed', stage='Falha', percent=None, error_code=code)
            finally:
                with lock:
                    processes.pop(job_id, None)
                shutil.rmtree(item_folder, ignore_errors=True)
            set_job(job_id, percent=round(position / total * 100, 2))
        if playlist_cancelled(job_id):
            raise RuntimeError('CANCELED')
        if not successes:
            raise RuntimeError('PLAYLIST_FAILED')
        set_job(job_id, stage='Criando ZIP', percent=None)
        archive = folder / 'media.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as out:
            for _, path in successes:
                out.write(path, arcname=path.name)
        if archive.stat().st_size > MAX_PLAYLIST_TOTAL_BYTES:
            raise RuntimeError('PLAYLIST_TOTAL_LIMIT')
        for _, path in successes:
            path.unlink(missing_ok=True)
        finished = epoch()
        with db() as c:
            c.execute("""UPDATE jobs SET status='completed',stage=?,percent=100,
                         filename=?,filesize=?,completed=?,expires=? WHERE id=?""",
                      (f'Concluído · {len(successes)}/{total}', safe_filename(row['title'], 'zip'),
                       archive.stat().st_size, finished, finished + EXPIRES_SECONDS, job_id))
    except Exception as exc:
        code = str(exc) if str(exc) in MESSAGES else 'PROCESSING'
        set_job(job_id, status='canceled' if code == 'CANCELED' else 'failed',
                stage='Cancelado' if code == 'CANCELED' else 'Falha', percent=None, error_code=code)
        if code == 'CANCELED':
            with db() as c:
                c.execute("""UPDATE playlist_items SET status='canceled',stage='Cancelado',error_code='CANCELED'
                             WHERE job_id=? AND status IN ('queued','running')""", (job_id,))
        shutil.rmtree(folder, ignore_errors=True)


def folder_tree_size(folder: Path) -> int:
    try:
        return sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
    except (FileNotFoundError, OSError):
        return 0


def worker():
    while True:
        with db() as c:
            job = claim_next(sys.modules[__name__], c)
        if job:
            try:
                (execute_playlist_job if job['kind'] == 'playlist' else execute_job)(job)
            except Exception:
                set_job(job['id'],status='failed',stage='Falha',error_code='PROCESSING')
                if job['parent_id']:
                    refresh_parent(sys.modules[__name__], job['parent_id'])
        else:
            time.sleep(.4)


def cleanup():
    while True:
        now = epoch()
        with db() as c:
            rows = c.execute("SELECT id FROM jobs WHERE status='completed' AND expires <= ?", (now,)).fetchall()
            for row in rows:
                c.execute("UPDATE jobs SET status='expired',stage='Expirado',error_code='EXPIRED' WHERE id=?", (row['id'],))
            c.execute('DELETE FROM inspections WHERE created < ?', (now - INSPECT_TTL,))
            c.execute('DELETE FROM playlist_items WHERE job_id IN (SELECT id FROM jobs WHERE created < ? AND status IN (\'expired\',\'failed\',\'canceled\'))', (now - 86400 * JOB_RETENTION_DAYS,))
            c.execute('DELETE FROM jobs WHERE created < ? AND status IN (\'expired\',\'failed\',\'canceled\')', (now - 86400 * JOB_RETENTION_DAYS,))
        for row in rows:
            shutil.rmtree(WORK / row['id'], ignore_errors=True)
        admin_privacy_cleanup()
        time.sleep(60)


@app.on_event('startup')
def on_startup():
    global runner_started
    if runner_started:
        return
    init_db()
    for i in range(MAX_WORKERS):
        threading.Thread(target=worker, name=f'media-worker-{i+1}', daemon=True).start()
    threading.Thread(target=cleanup, name='media-cleanup', daemon=True).start()
    runner_started = True


from .batch import split_links, inspect_collection, create_batch, batch_status, cancel_batch, claim_next, refresh_parent, MAX_WORKERS

@app.get('/api/capabilities')
def capabilities():
    return {'maxConcurrency': MAX_PARALLEL_DOWNLOADS, 'maxBatchItems':MAX_PLAYLIST_ITEMS}

from .admin import install_admin
install_admin(app, sys.modules[__name__])

app.mount('/', StaticFiles(directory=str(WEB), html=True), name='web')