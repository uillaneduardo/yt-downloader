"""Private administration API. Public download tokens never grant administrative access."""
from __future__ import annotations

import hashlib
import hmac
import json
import ipaddress
import os
import secrets
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

# Setting name -> (minimum, maximum). Guard storage/CPU against accidental unlimited input.
BOUNDS = {
    'MAX_FILE_MB': (16, 20480),
    'MAX_TEMP_MB': (64, 65536),
    'MAX_PLAYLIST_TOTAL_MB': (64, 40960),
    'MAX_PLAYLIST_TEMP_MB': (128, 131072),
    'MAX_PLAYLIST_ITEMS': (1, 50),
    'MAX_DURATION_SECONDS': (60, 86400),
    'JOB_TIMEOUT_SECONDS': (60, 86400),
    'FILE_TTL_SECONDS': (300, 604800),
    'IP_RETENTION_DAYS': (1, 30),
    'JOB_RETENTION_DAYS': (7, 365),
}
ALIASES = {'MAX_DURATION_SECONDS':'MAX_DURATION', 'JOB_TIMEOUT_SECONDS':'JOB_TIMEOUT', 'FILE_TTL_SECONDS':'EXPIRES_SECONDS'}
COOKIE = 'yt_admin_session'
COOKIE_PATH = '/api/admin'
SESSION_LIFE = 12 * 3600


class Credentials(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class LimitsBody(BaseModel):
    limits: dict[str, int]


def install_admin(app, m):
    """Attach API to the existing one-worker application, using its SQLite connection."""
    def admin_password():
        value = os.getenv('ADMIN_PASSWORD', '')
        return value if len(value) >= 16 else None

    def fail(code, message, status=400):
        raise HTTPException(status, detail={'code': code, 'message': message})

    def current_limits():
        return {key: int(getattr(m, ALIASES.get(key, key))) for key in BOUNDS}

    def get_ip(request):
        # Only trust Cloudflare's header when the deployment deliberately opts in.
        raw = request.client.host if request.client else ''
        if os.getenv('TRUST_CF_IP') == '1':
            raw = request.headers.get('cf-connecting-ip') or raw
        try:
            addr = ipaddress.ip_address(raw)
            if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
                addr = addr.ipv4_mapped
            return str(addr)
        except (ValueError, AttributeError):
            return None

    m.admin_client_ip = get_ip

    def initialize_schema():
        with m.db() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY, csrf_hash TEXT NOT NULL,
                created INTEGER NOT NULL, expires INTEGER NOT NULL
              );
              CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires ON admin_sessions(expires);
            ''')
            cols = {r['name'] for r in c.execute('PRAGMA table_info(jobs)')}
            if 'client_ip' not in cols:
                c.execute('ALTER TABLE jobs ADD COLUMN client_ip TEXT')
            settings = c.execute('SELECT key,value FROM admin_settings').fetchall()
        loaded = {r['key']: int(r['value']) for r in settings if r['key'] in BOUNDS}
        if loaded:
            apply_limits(loaded, persist=False)

    def apply_limits(updates, *, persist=True):
        with m.lock:
            limits = current_limits()
            limits.update(updates)
            if limits['MAX_TEMP_MB'] < limits['MAX_FILE_MB'] * 2:
                fail('INVALID_LIMITS', 'O espaço temporário individual deve ser pelo menos o dobro do limite final.', 422)
            if limits['MAX_PLAYLIST_TEMP_MB'] < limits['MAX_PLAYLIST_TOTAL_MB'] * 2:
                fail('INVALID_LIMITS', 'O espaço temporário de playlist deve ser pelo menos o dobro do limite do ZIP.', 422)
            for key, value in updates.items():
                setattr(m, ALIASES.get(key, key), value)
            m.MAX_BYTES = m.MAX_FILE_MB * 1048576
            m.MAX_TEMP_BYTES = m.MAX_TEMP_MB * 1048576
            m.MAX_PLAYLIST_TOTAL_BYTES = m.MAX_PLAYLIST_TOTAL_MB * 1048576
            m.MAX_PLAYLIST_TEMP_BYTES = m.MAX_PLAYLIST_TEMP_MB * 1048576
            m.MESSAGES['TOO_LARGE'] = f'O arquivo excedeu o limite de {m.MAX_FILE_MB} MB. Escolha uma qualidade menor.'
            m.MESSAGES['PLAYLIST_TOTAL_LIMIT'] = f'A playlist excedeu o limite total de {m.MAX_PLAYLIST_TOTAL_MB} MB.'
            if persist:
                with m.db() as c:
                    c.executemany('INSERT OR REPLACE INTO admin_settings(key,value) VALUES(?,?)', list(updates.items()))

    def ensure_admin(request: Request, *, mutate=False):
        if not admin_password():
            fail('ADMIN_DISABLED', 'Defina ADMIN_PASSWORD com pelo menos 16 caracteres no arquivo .env.', 503)
        token = request.cookies.get(COOKIE, '')
        if len(token) != 64:
            fail('UNAUTHORIZED', 'Faça login para continuar.', 401)
        hashed = hashlib.sha256(token.encode()).hexdigest()
        with m.db() as c:
            row = c.execute('SELECT csrf_hash,expires FROM admin_sessions WHERE token_hash=?', (hashed,)).fetchone()
        if not row or row['expires'] <= m.epoch():
            fail('UNAUTHORIZED', 'Sessão encerrada. Entre novamente.', 401)
        if mutate:
            origin = request.headers.get('origin', '')
            if origin:
                parsed = urlparse(origin)
                if parsed.netloc != request.headers.get('host') or parsed.scheme not in ('http', 'https'):
                    fail('FORBIDDEN', 'Solicitação recusada.', 403)
            provided = request.headers.get('x-admin-csrf', '')
            if not hmac.compare_digest(hashlib.sha256(provided.encode()).hexdigest(), row['csrf_hash']):
                fail('FORBIDDEN', 'Atualize a página e tente novamente.', 403)
        return hashed

    @app.get('/admin')
    @app.get('/admin/')
    def admin_page():
        return FileResponse(m.WEB / 'admin.html', headers={'Cache-Control': 'no-store'})

    @app.get('/api/admin/session')
    def admin_session(request: Request):
        if not admin_password():
            return {'enabled': False, 'authenticated': False}
        try:
            hashed = ensure_admin(request)
            csrf = secrets.token_urlsafe(32)
            with m.db() as c:
                c.execute('UPDATE admin_sessions SET csrf_hash=? WHERE token_hash=?',
                          (hashlib.sha256(csrf.encode()).hexdigest(), hashed))
            return {'enabled': True, 'authenticated': True, 'csrf': csrf}
        except HTTPException:
            return {'enabled': True, 'authenticated': False}

    @app.post('/api/admin/login')
    def login(body: Credentials, request: Request):
        pwd = admin_password()
        if not pwd:
            fail('ADMIN_DISABLED', 'Configure ADMIN_PASSWORD no homelab.', 503)
        m.allow_rate(request, 5, 900)
        if not hmac.compare_digest(body.password.encode(), pwd.encode()):
            fail('UNAUTHORIZED', 'Senha incorreta.', 401)
        token, csrf = secrets.token_hex(32), secrets.token_urlsafe(32)
        with m.db() as c:
            c.execute('INSERT INTO admin_sessions VALUES(?,?,?,?)',
                      (hashlib.sha256(token.encode()).hexdigest(),
                       hashlib.sha256(csrf.encode()).hexdigest(), m.epoch(), m.epoch() + SESSION_LIFE))
        response = JSONResponse({'authenticated': True, 'csrf': csrf})
        host = request.url.hostname or ''
        response.set_cookie(COOKIE, token, max_age=SESSION_LIFE, httponly=True,
                            secure=host not in ('localhost', '127.0.0.1', 'testserver'),
                            samesite='strict', path=COOKIE_PATH)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.post('/api/admin/logout')
    def logout(request: Request):
        hashed = ensure_admin(request, mutate=True)
        with m.db() as c:
            c.execute('DELETE FROM admin_sessions WHERE token_hash=?', (hashed,))
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE, path=COOKIE_PATH)
        return response

    @app.get('/api/admin/overview')
    def overview(request: Request):
        ensure_admin(request)
        with m.db() as c:
            job_rows = c.execute('SELECT status,count(*) n FROM jobs GROUP BY status').fetchall()
            recent = c.execute('SELECT count(*) n FROM jobs WHERE created >= ?', (m.epoch()-86400,)).fetchone()['n']
            failed = c.execute("SELECT count(*) n FROM jobs WHERE status='failed' AND created >= ?", (m.epoch()-86400,)).fetchone()['n']
        disk = shutil.disk_usage(m.DATA)
        folders = [p for p in m.WORK.iterdir() if p.is_dir() and not p.is_symlink()]
        used = sum(m.folder_tree_size(p) for p in folders)
        return {'jobs': {r['status']: r['n'] for r in job_rows}, 'last24h': recent,
                'failed24h': failed, 'tempBytes': used, 'freeBytes': disk.free,
                'diskTotalBytes': disk.total, 'folderCount': len(folders),
                'limits': current_limits(), 'version': m.APP_VERSION}

    @app.get('/api/admin/jobs')
    def admin_jobs(request: Request, limit: int=40, offset: int=0, status: str='', q: str=''):
        ensure_admin(request)
        limit = max(1, min(100, limit)); offset = max(0, min(100000, offset)); q = q[:120].strip()
        conditions = []; args = []
        if status:
            if status not in ('queued','running','completed','failed','canceled','expired'):
                fail('INVALID_STATUS', 'Filtro inválido.', 422)
            conditions.append('status=?'); args.append(status)
        if q:
            conditions.append('(title LIKE ? OR url LIKE ? OR client_ip LIKE ?)')
            args.extend(['%' + q + '%'] * 3)
        where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
        with m.db() as c:
            total = c.execute('SELECT count(*) n FROM jobs' + where, args).fetchone()['n']
            rows = c.execute('''SELECT id,kind,title,url,source,status,stage,profile,client_ip,
                                      error_code,created,completed,filesize,filename,expires
                               FROM jobs''' + where + ' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?', args+[limit,offset]).fetchall()
        return {'total':total, 'items': [
            {**{key: row[key] for key in row.keys() if key != 'profile'},
             'profile': json.loads(row['profile']),
             'errorMessage': m.MESSAGES.get(row['error_code']) if row['error_code'] else None}
            for row in rows]}

    @app.get('/api/admin/storage')
    def storage(request: Request):
        ensure_admin(request)
        with m.db() as c:
            jobs = {r['id']: dict(r) for r in c.execute('SELECT id,title,status,expires FROM jobs').fetchall()}
        items = []
        for path in m.WORK.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            size = m.folder_tree_size(path)
            job = jobs.get(path.name, {})
            items.append({'id':path.name, 'title':job.get('title') or 'Arquivos órfãos',
                          'status':job.get('status') or 'orphan', 'expires':job.get('expires'),
                          'sizeBytes':size, 'files':sum(p.is_file() for p in path.rglob('*'))})
        items.sort(key=lambda x: x['sizeBytes'], reverse=True)
        return {'items': items[:200], 'totalBytes': sum(x['sizeBytes'] for x in items)}

    @app.get('/api/admin/limits')
    def limits(request: Request):
        ensure_admin(request)
        return {'limits':current_limits(), 'bounds': {k:list(v) for k,v in BOUNDS.items()}}

    @app.put('/api/admin/limits')
    def update_limits(body: LimitsBody, request: Request):
        ensure_admin(request, mutate=True)
        if not body.limits:
            fail('INVALID_LIMITS', 'Informe pelo menos um limite.', 422)
        for key, value in body.limits.items():
            if key not in BOUNDS or isinstance(value, bool) or not isinstance(value, int):
                fail('INVALID_LIMITS', 'Parâmetro inválido.', 422)
            low, high = BOUNDS[key]
            if not low <= value <= high:
                fail('INVALID_LIMITS', f'{key}: valor permitido entre {low} e {high}.', 422)
        apply_limits(body.limits)
        return {'limits':current_limits(), 'message':'Limites atualizados.'}

    @app.delete('/api/admin/jobs/{job_id}/files')
    def delete_files(job_id: str, request: Request):
        ensure_admin(request, mutate=True)
        if not len(job_id) == 32 or any(ch not in '0123456789abcdef' for ch in job_id):
            fail('NOT_FOUND', 'Registro não encontrado.', 404)
        with m.db() as c:
            row = c.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                fail('NOT_FOUND', 'Registro não encontrado.', 404)
            if row['status'] in ('queued','running'):
                fail('BUSY', 'Não é possível excluir um download em andamento.', 409)
            c.execute("UPDATE jobs SET status='expired',stage='Removido',expires=?,filesize=NULL,filename=NULL WHERE id=?",(m.epoch(), job_id))
        shutil.rmtree(m.WORK / job_id, ignore_errors=True)
        return {'ok': True}

    @app.post('/api/admin/cleanup')
    def cleanup_now(request: Request):
        ensure_admin(request, mutate=True)
        now = m.epoch(); remove = []
        with m.db() as c:
            rows = c.execute("SELECT id FROM jobs WHERE status='completed' AND expires<=?", (now,)).fetchall()
            for row in rows:
                c.execute("UPDATE jobs SET status='expired',stage='Expirado' WHERE id=?", (row['id'],))
            remove = [r['id'] for r in rows]
            active = {r['id'] for r in c.execute("SELECT id FROM jobs WHERE status IN ('queued','running','completed')")}
        for p in m.WORK.iterdir():
            if not p.is_dir() or p.is_symlink():
                continue
            if p.name not in active:
                remove.append(p.name)
        removed = 0
        for id in set(remove):
            if len(id)==32 and all(c in '0123456789abcdef' for c in id):
                path = m.WORK / id
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
        return {'ok': True, 'removed': removed}

    def privacy_cleanup():
        now = m.epoch()
        with m.db() as c:
            c.execute('UPDATE jobs SET client_ip=NULL WHERE client_ip IS NOT NULL AND created < ?',
                      (now-86400*m.IP_RETENTION_DAYS,))
            c.execute('DELETE FROM admin_sessions WHERE expires < ?', (now,))
    m.admin_schema = initialize_schema
    m.admin_privacy_cleanup = privacy_cleanup
