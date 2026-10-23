import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

# The production DB should never be touched by tests.
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='yt-test-')

from app import main  # noqa: E402


def fake_info(_url):
    return {'title':'Teste de mídia', 'author':'Canal', 'duration': 123, 'thumbnail':'',
            'profiles': [{'id':'video-mp4-720','kind':'video','label':'720p','ext':'MP4','height':720},
                         {'id':'audio-mp3-192','kind':'audio','label':'192 kbps','ext':'MP3'}]}


def test_url_validation():
    assert main.normalized_url('https://youtu.be/aqz-KE-bpKQ')[0] == 'https://www.youtube.com/watch?v=aqz-KE-bpKQ'
    assert main.normalized_url('https://www.youtube.com/watch?v=aqz-KE-bpKQ&list=evil')[0].endswith('aqz-KE-bpKQ')
    assert main.normalized_url('https://vimeo.com/12345')[1] == 'Vimeo'
    for bad in ['http://vimeo.com/123', 'https://localhost/123', 'https://evil.com/?url=youtube.com',
                'https://youtube.com.evil.com/watch?v=aqz-KE-bpKQ', 'file:///etc/passwd',
                'https://www.youtube.com/watch?v=bad', 'https://127.0.0.1/']:
        try:
            main.normalized_url(bad)
            assert False, bad
        except Exception as exc:
            assert getattr(exc, 'status_code', None) in (400, 422), bad


def test_profiles_are_sourced_from_formats():
    formats = [{'height': 720, 'vcodec': 'vp9', 'acodec': 'none'},
               {'height': None, 'vcodec': 'none', 'acodec': 'opus'}]
    out = main.profiles_from({'formats': formats})
    assert [p['id'] for p in out if p['kind']=='video'] == ['video-mp4-720']
    assert len([p for p in out if p['kind']=='audio']) == 3
    assert main.profiles_from({'formats': []}) == []


def test_inspect_queue_auth_history(monkeypatch):
    monkeypatch.setattr(main, 'inspect_media', fake_info)
    with TestClient(main.app) as c:
        assert c.get('/api/health').json()['status'] == 'ok'
        r = c.post('/api/inspect', json={'url':'https://youtu.be/aqz-KE-bpKQ'})
        assert r.status_code == 200, r.text
        inspection = r.json()['inspectionId']
        no = c.post('/api/downloads', json={'inspectionId': inspection, 'profileId':'fake-profile'})
        assert no.status_code == 422
        # Worker is intentionally not invoked in this test: create an example job with a blocked subprocess.
        monkeypatch.setattr(main, 'execute_job', lambda row: None)
        r = c.post('/api/downloads', json={'inspectionId': inspection, 'profileId':'video-mp4-720'})
        assert r.status_code == 202
        job = r.json()
        assert c.get(f'/api/downloads/{job["id"]}', params={'token':'wrong'}).status_code == 404
        assert c.get(f'/api/downloads/{job["id"]}', params={'token':job['token']}).status_code == 200
        assert c.get('/api/history').json()['items'] == []
        assert c.post(f'/api/downloads/{job["id"]}/cancel', params={'token':job['token']}).status_code == 200


def test_filename_sanitization():
    assert main.safe_filename('..///Arquivo: teste\\?', 'MP3') == 'Arquivo teste.mp3'
    assert len(main.safe_filename('a' * 300, 'mp4')) <= 115


def test_inspect_response_and_safe_unavailable(monkeypatch):
    import json
    from types import SimpleNamespace
    info = {
        'title': 'Vídeo público', 'uploader': 'Criador', 'duration': 120,
        'thumbnail': 'https://i.ytimg.com/example.jpg', 'availability': 'public',
        'formats': [
            {'height': 720, 'vcodec': 'avc1', 'acodec': 'none'},
            {'height': None, 'vcodec': 'none', 'acodec': 'mp4a'},
        ],
    }
    monkeypatch.setattr(main.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=json.dumps(info), stderr=''))
    result = main.inspect_media('https://www.youtube.com/watch?v=aqz-KE-bpKQ')
    assert result['publicEligible'] is True
    assert result['title'] == 'Vídeo público'
    assert any(p['id'] == 'video-mp4-720' for p in result['profiles'])
    assert any(p['id'] == 'audio-m4a-best' for p in result['profiles'])
    info['availability'] = 'unlisted'
    result = main.inspect_media('https://www.youtube.com/watch?v=aqz-KE-bpKQ')
    assert result['publicEligible'] is False
    info['duration'] = 0
    try:
        main.inspect_media('https://www.youtube.com/watch?v=aqz-KE-bpKQ')
        assert False, 'Expected media unavailable'
    except Exception as exc:
        assert getattr(exc, 'status_code', 0) == 422


def test_worker_completion_file_and_public_history(monkeypatch):
    import json
    import uuid
    import hashlib
    main.init_db()
    job_id = uuid.uuid4().hex
    token = 'local-test-secret-token'
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    profile = {'id': 'audio-mp3-192', 'kind': 'audio', 'ext': 'MP3', 'label': '192 kbps'}
    with main.db() as c:
        c.execute('''INSERT INTO jobs (id, token_hash, url, title, profile, metadata, publish,
            source, status, stage, percent, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (job_id, token_hash, 'https://www.youtube.com/watch?v=aqz-KE-bpKQ',
             'Título de Teste', json.dumps(profile), 1, 1, 'YouTube',
             'running', 'Obtendo arquivo', None, main.epoch()))

    class FakeProcess:
        pid = 999999

        def __init__(self, command, **kwargs):
            assert '--embed-metadata' in command
            assert '--embed-thumbnail' in command
            assert '--audio-format' in command and 'mp3' in command
            assert '--audio-quality' in command and '192K' in command
            Path(kwargs['cwd'], 'media.mp3').write_bytes(b'ID3mock-audio-data')
            self.stdout = iter(['[download] DL:49.5%|50|100|NA', '[ExtractAudio] processing'])

        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(main.subprocess, 'Popen', FakeProcess)
    with main.db() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    main.execute_job(row)
    with main.db() as c:
        result = c.execute('SELECT status, filename, filesize, percent FROM jobs WHERE id=?', (job_id,)).fetchone()
        history = c.execute('SELECT url, source FROM public_history WHERE url=?',
                            ('https://www.youtube.com/watch?v=aqz-KE-bpKQ',)).fetchall()
    assert result['status'] == 'completed'
    assert result['filename'] == 'Título de Teste.mp3'
    assert result['percent'] == 100
    assert len(history) >= 1
    with TestClient(main.app) as client:
        assert client.get(f'/api/downloads/{job_id}/file', params={'token':'wrong'}).status_code == 404
        answer = client.get(f'/api/downloads/{job_id}/file', params={'token':token})
        assert answer.status_code == 200
        assert answer.content == b'ID3mock-audio-data'
        assert 'audio/mpeg' in answer.headers['content-type']


def test_client_ip_header_requires_explicit_trust(monkeypatch):
    from starlette.requests import Request
    scope = {'type':'http', 'method':'GET', 'scheme':'http', 'path':'/api/inspect',
             'headers':[(b'cf-connecting-ip', b'192.0.2.10')],
             'client':('127.0.0.1', 1234), 'server':('localhost', 8000), 'query_string': b''}
    request = Request(scope)
    monkeypatch.delenv('TRUST_CF_IP', raising=False)
    baseline = main.client_key(request)
    monkeypatch.setenv('TRUST_CF_IP', '1')
    assert main.client_key(request) != baseline
    scope['headers'] = [(b'cf-connecting-ip', b'not-an-ip')]
    assert main.client_key(Request(scope)) == baseline


def playlist_example(monkeypatch, count=3):
    from types import SimpleNamespace
    import json
    playlist = {
        '_type': 'playlist', 'title': 'Minha playlist', 'uploader': 'Canal de exemplo',
        'entries': [{'id': f'ABcd1234{i:03d}', 'title': f'Episódio {i+1}'} for i in range(count)],
    }
    calls = []
    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps(playlist), stderr='')
    monkeypatch.setattr(main.subprocess, 'run', fake_run)
    return calls


def test_playlist_url_and_inspection(monkeypatch):
    from fastapi import HTTPException
    normalized, source = main.normalized_url('https://music.youtube.com/playlist?list=PLabcdefgh123456&feature=share')
    assert source == 'YouTube'
    assert normalized == 'https://www.youtube.com/playlist?list=PLabcdefgh123456'
    assert main.normalized_url('https://www.youtube.com/watch?v=aqz-KE-bpKQ&list=PLabcdefgh123456')[0].endswith('aqz-KE-bpKQ')
    for bad in ['https://youtube.com/playlist?list=short', 'https://youtube.com/playlist?list=PLabc%0aevil',
                'https://youtube.com/playlist?list=PLabcdefgh123456&list=also',
                'https://vimeo.com/playlist?list=PLabcdefgh123456']:
        if '&list=also' in bad:
            # Only one canonical parameter is used even when a URL contains duplicates.
            assert main.normalized_url(bad)[0] == 'https://www.youtube.com/playlist?list=PLabcdefgh123456'
        else:
            try:
                main.normalized_url(bad)
                assert False, bad
            except HTTPException:
                pass
    calls = playlist_example(monkeypatch)
    info = main.inspect_playlist(normalized)
    assert info['kind'] == 'playlist'
    assert info['count'] == 3
    assert info['publicEligible'] is False
    assert info['entries'][0]['url'] == 'https://www.youtube.com/watch?v=ABcd1234000'
    assert '--flat-playlist' in calls[0]
    assert '--playlist-end' in calls[0]


def test_playlist_rejects_oversized_and_empty(monkeypatch):
    from fastapi import HTTPException
    playlist_example(monkeypatch, main.MAX_PLAYLIST_ITEMS + 1)
    try:
        main.inspect_playlist('https://www.youtube.com/playlist?list=PLabcdefgh123456')
        assert False
    except HTTPException as exc:
        assert exc.status_code == 413
        assert exc.detail['code'] == 'PLAYLIST_LIMIT'
    from types import SimpleNamespace
    import json
    monkeypatch.setattr(main.subprocess, 'run', lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stderr='', stdout=json.dumps({'_type': 'playlist', 'entries':[{'id':'bad'}]})))
    try:
        main.inspect_playlist('https://www.youtube.com/playlist?list=PLabcdefgh123456')
        assert False
    except HTTPException as exc:
        assert exc.detail['code'] == 'PLAYLIST_EMPTY'


def test_playlist_create_status_and_publish_rejection(monkeypatch):
    def fake_playlist(_url):
        return {'kind':'playlist', 'title':'Minha playlist', 'author':'Criador', 'duration':None,
                'thumbnail':'', 'publicEligible':False,
                'entries':[{'id':f'ABcd1234{i:03d}', 'url':f'https://www.youtube.com/watch?v=ABcd1234{i:03d}',
                            'title':f'Item {i}'} for i in range(3)], 'count':3,
                'profiles':[{'id':'video-mp4-720','kind':'video','ext':'MP4','height':720,'label':'Até 720p'}]}
    monkeypatch.setattr(main, 'inspect_playlist', fake_playlist)
    monkeypatch.setattr(main, 'execute_job', lambda _row: None)
    with TestClient(main.app) as c:
        i = c.post('/api/inspect', json={'url':'https://www.youtube.com/playlist?list=PLabcdefgh123456'})
        assert i.status_code == 200, i.text
        assert i.json()['kind'] == 'batch'
        iid = i.json()['inspectionId']
        denied = c.post('/api/downloads',json={'inspectionId':iid,'profileId':'video-mp4-720','publish':True,'selectedPositions':[1]})
        assert denied.status_code == 422
        r = c.post('/api/downloads',json={'inspectionId':iid,'profileId':'video-mp4-720','selectedPositions':[1,3],'concurrency':1})
        assert r.status_code == 202, r.text
        job = r.json()
        assert c.get(f'/api/downloads/{job["id"]}',params={'token':'wrong'}).status_code == 404
        status = c.get(f'/api/downloads/{job["id"]}',params={'token':job['token']}).json()
        assert status['kind'] == 'batch'
        assert status['batch']['total'] == 2
        assert [entry['position'] for entry in status['batch']['items']] == [1,3]
        assert c.get(f'/api/downloads/{job["id"]}/file',params={'token':job['token']}).status_code == 410
        assert c.post(f'/api/downloads/{job["id"]}/cancel',params={'token':job['token']}).status_code == 200


def build_test_playlist_job(count=3):
    import uuid, hashlib, json
    main.init_db()
    job_id = uuid.uuid4().hex
    token = 'playlist-secret'
    profile = {'id':'video-mp4-720', 'kind':'video', 'ext':'MP4', 'height':720, 'label':'Até 720p'}
    with main.db() as c:
        c.execute('''INSERT INTO jobs (id,token_hash,url,title,profile,metadata,publish,source,
                     status,stage,percent,created,kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (job_id,hashlib.sha256(token.encode()).hexdigest(),
                   'https://www.youtube.com/playlist?list=PLabcdefgh123456','Lista de testes',
                   json.dumps(profile),1,0,'YouTube','running','Obtendo',None,main.epoch(),'playlist'))
        for i in range(count):
            c.execute('INSERT INTO playlist_items(job_id,position,video_id,title,url) VALUES (?,?,?,?,?)',
                      (job_id,i+1,f'ABcd1234{i:03d}', f'Vídeo {i+1}',
                       f'https://www.youtube.com/watch?v=ABcd1234{i:03d}'))
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    return job_id, token, row


def test_playlist_sequential_archive_and_partial_failure(monkeypatch):
    import zipfile
    job_id, token, row = build_test_playlist_job()
    visited = []
    class FakeProcess:
        pid = 999999
        def __init__(self, command, **kwargs):
            url = command[-1]
            visited.append(url)
            assert '--no-playlist' in command
            assert '--match-filter' in command
            assert '--embed-thumbnail' in command
            self.failed = url.endswith('001')
            if not self.failed:
                Path(kwargs['cwd'], 'media.mp4').write_bytes(url.encode())
            self.stdout = iter(['[download] DL:45.0%|45|100|NA'] if not self.failed else ['ERROR: Video unavailable'])
        def poll(self): return 1 if self.failed else 0
        def wait(self): return 1 if self.failed else 0
    monkeypatch.setattr(main.subprocess, 'Popen', FakeProcess)
    main.execute_playlist_job(row)
    assert visited == [f'https://www.youtube.com/watch?v=ABcd1234{i:03d}' for i in range(3)]
    with main.db() as c:
        result = c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        items = c.execute('SELECT * FROM playlist_items WHERE job_id=? ORDER BY position',(job_id,)).fetchall()
    assert result['status'] == 'completed'
    assert result['percent'] == 100
    assert [x['status'] for x in items] == ['completed','failed','completed']
    archive = main.WORK / job_id / 'media.zip'
    with zipfile.ZipFile(archive) as z:
        assert z.namelist() == ['01 - Vídeo 1.mp4','03 - Vídeo 3.mp4']
    assert len(list((main.WORK / job_id).glob('*.mp4'))) == 0
    with TestClient(main.app) as c:
        ok = c.get(f'/api/downloads/{job_id}/file',params={'token':token})
        assert ok.status_code == 200
        assert ok.headers['content-type'].startswith('application/zip')
        s = c.get(f'/api/downloads/{job_id}',params={'token':token}).json()
        assert s['playlist']['completed'] == 2 and s['playlist']['failed'] == 1


def test_playlist_cancel_skips_all_items(monkeypatch):
    job_id, _token, row = build_test_playlist_job(2)
    with main.db() as c:
        c.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?',(job_id,))
    monkeypatch.setattr(main.subprocess,'Popen',lambda *_a,**_kw: (_ for _ in ()).throw(AssertionError('Should not start subprocess')))
    main.execute_playlist_job(row)
    with main.db() as c:
        job = c.execute('SELECT status FROM jobs WHERE id=?',(job_id,)).fetchone()
        statuses = c.execute('SELECT status FROM playlist_items WHERE job_id=?',(job_id,)).fetchall()
    assert job['status'] == 'canceled'
    assert [x['status'] for x in statuses] == ['canceled','canceled']


def test_ytdlp_size_and_storage_errors_are_classified():
    assert main.failure_code('ERROR: File is larger than max-filesize (1234 > 1000)') == 'TOO_LARGE'
    assert main.failure_code('ERROR: [Errno 28] No space left on device') == 'NO_SPACE'
    assert '1024' in main.MESSAGES['TOO_LARGE'] or str(main.MAX_FILE_MB) in main.MESSAGES['TOO_LARGE']


def test_intermediate_video_and_audio_do_not_count_as_final_size(monkeypatch):
    import hashlib
    import json
    import uuid
    job_id = uuid.uuid4().hex
    profile = {'kind': 'video', 'ext': 'MP4', 'height': 720, 'label': '720p'}
    main.init_db()
    with main.db() as c:
        c.execute('''INSERT INTO jobs (id, token_hash, url, title, profile, metadata, publish,
            source, status, stage, percent, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (job_id, hashlib.sha256(b'token').hexdigest(), 'https://www.youtube.com/watch?v=aqz-KE-bpKQ',
             'Arquivos intermediários', json.dumps(profile), 0, 0, 'YouTube', 'running', 'Obtendo', None, main.epoch()))
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    monkeypatch.setattr(main, 'MAX_BYTES', 100)
    monkeypatch.setattr(main, 'MAX_TEMP_BYTES', 1024)

    class MockProcess:
        pid = 999999
        def __init__(self, command, **kwargs):
            folder = Path(kwargs['cwd'])
            (folder / 'media.mp4').write_bytes(b'video' * 10)  # output: 50 bytes
            (folder / 'media.webm').write_bytes(b'video-stream' * 10)  # intermediate: 120 bytes
            (folder / 'media.m4a').write_bytes(b'audio-stream' * 10)  # intermediate: 120 bytes
            self.stdout = iter(['[download] DL:100.0%|100|100|NA', '[Merger] merged'])
        def poll(self): return 0
        def wait(self): return 0

    monkeypatch.setattr(main.subprocess, 'Popen', MockProcess)
    main.execute_job(row)
    with main.db() as c:
        result = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    assert result['status'] == 'completed', result['error_code']
    assert result['filesize'] == 50
    assert sorted(path.name for path in (main.WORK / job_id).iterdir()) == ['media.mp4']


def test_actual_oversized_output_remains_blocked(monkeypatch):
    import hashlib
    import json
    import uuid
    job_id = uuid.uuid4().hex
    profile = {'kind': 'video', 'ext': 'MP4', 'height': 720, 'label': '720p'}
    main.init_db()
    with main.db() as c:
        c.execute('''INSERT INTO jobs (id, token_hash, url, title, profile, metadata, publish,
            source, status, stage, percent, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (job_id, hashlib.sha256(b'token').hexdigest(), 'https://www.youtube.com/watch?v=aqz-KE-bpKQ',
             'Arquivo acima limite', json.dumps(profile), 0, 0, 'YouTube', 'running', 'Obtendo', None, main.epoch()))
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    monkeypatch.setattr(main, 'MAX_BYTES', 100)
    monkeypatch.setattr(main, 'MAX_TEMP_BYTES', 1024)

    class MockProcess:
        pid = 999999
        def __init__(self, command, **kwargs):
            (Path(kwargs['cwd']) / 'media.mp4').write_bytes(b'video' * 25)
            self.stdout = iter(['[Merger] merged'])
        def poll(self): return 0
        def wait(self): return 0

    monkeypatch.setattr(main.subprocess, 'Popen', MockProcess)
    main.execute_job(row)
    with main.db() as c:
        result = c.execute('SELECT status,error_code FROM jobs WHERE id=?', (job_id,)).fetchone()
    assert result['status'] == 'failed'
    assert result['error_code'] == 'TOO_LARGE'


def test_playlist_scratch_not_confused_with_zip_limit(monkeypatch):
    import zipfile
    job_id, token, row = build_test_playlist_job(count=2)
    monkeypatch.setattr(main, 'MAX_BYTES', 100)
    monkeypatch.setattr(main, 'MAX_TEMP_BYTES', 4096)
    monkeypatch.setattr(main, 'MAX_PLAYLIST_TOTAL_BYTES', 500)
    monkeypatch.setattr(main, 'MAX_PLAYLIST_TEMP_BYTES', 4096)

    class MockProcess:
        pid = 999999
        def __init__(self, command, **kwargs):
            folder = Path(kwargs['cwd'])
            (folder / 'media.mp4').write_bytes(b'x' * 30)
            (folder / 'media.webm').write_bytes(b'a' * 700)
            self.stdout = iter(['[Merger] merged'])
        def poll(self): return 0
        def wait(self): return 0

    monkeypatch.setattr(main.subprocess, 'Popen', MockProcess)
    main.execute_playlist_job(row)
    with main.db() as c:
        job = c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        statuses = c.execute('SELECT status FROM playlist_items WHERE job_id=? ORDER BY position',(job_id,)).fetchall()
    assert job['status'] == 'completed', job['error_code']
    assert [x['status'] for x in statuses] == ['completed','completed']
    with zipfile.ZipFile(main.WORK / job_id / 'media.zip') as archive:
        assert len(archive.namelist()) == 2


def test_admin_requires_password_and_session(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)
    with TestClient(main.app) as c:
        assert c.get('/api/admin/session').json() == {'enabled': False, 'authenticated': False}
        assert c.get('/api/admin/jobs').status_code == 503
        assert c.get('/admin').status_code == 200
    monkeypatch.setenv('ADMIN_PASSWORD', 'super-long-test-password-1234567')
    with TestClient(main.app) as c:
        assert c.get('/api/admin/jobs').status_code == 401
        assert c.post('/api/admin/login', json={'password':'bad'}).status_code == 401
        login = c.post('/api/admin/login', json={'password':'super-long-test-password-1234567'})
        assert login.status_code == 200
        csrf = login.json()['csrf']
        assert c.get('/api/admin/session').json()['authenticated'] is True
        # Session refresh rotates CSRF.
        csrf = c.get('/api/admin/session').json()['csrf']
        assert c.get('/api/admin/jobs').status_code == 200
        assert c.put('/api/admin/limits', json={'limits': {'MAX_FILE_MB':2048}}).status_code == 403
        ok = c.put('/api/admin/limits', headers={'X-Admin-CSRF':csrf}, json={'limits': {'MAX_FILE_MB':2048}})
        assert ok.status_code == 200, ok.text
        assert main.MAX_FILE_MB == 2048
        assert main.MAX_BYTES == 2048 * 1048576
        assert '2048' in main.MESSAGES['TOO_LARGE']
        bad = c.put('/api/admin/limits', headers={'X-Admin-CSRF':csrf}, json={'limits': {'MAX_TEMP_MB':64}})
        assert bad.status_code == 422
        # Sessions cannot be terminated without CSRF.
        assert c.post('/api/admin/logout', json={}).status_code == 403
        assert c.post('/api/admin/logout', json={}, headers={'X-Admin-CSRF':csrf}).status_code == 200
        assert c.get('/api/admin/jobs').status_code == 401
    monkeypatch.setattr(main, 'MAX_FILE_MB', 1024)
    monkeypatch.setattr(main, 'MAX_BYTES', 1024 * 1048576)


def test_admin_ip_file_management_and_privacy(monkeypatch):
    import json, uuid, hashlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv('ADMIN_PASSWORD', 'super-long-test-password-1234567')
    monkeypatch.setattr(main, 'inspect_media', fake_info)
    monkeypatch.setenv('TRUST_CF_IP', '1')
    with TestClient(main.app) as c:
        inspection = c.post('/api/inspect', json={'url':'https://youtu.be/aqz-KE-bpKQ'}).json()['inspectionId']
        create = c.post('/api/downloads',headers={'cf-connecting-ip':'198.51.100.8'}, json={'inspectionId':inspection, 'profileId':'video-mp4-720'})
        assert create.status_code == 202, create.text
        job_id = create.json()['id']
        login = c.post('/api/admin/login',json={'password':'super-long-test-password-1234567'})
        csrf = login.json()['csrf']
        rows = c.get('/api/admin/jobs').json()['items']
        row = next(r for r in rows if r['id'] == job_id)
        assert row['client_ip'] == '198.51.100.8'
        assert row['url'].endswith('aqz-KE-bpKQ')
        # Active files cannot be deleted from the admin dashboard.
        blocked = c.delete(f'/api/admin/jobs/{job_id}/files',headers={'X-Admin-CSRF':csrf})
        assert blocked.status_code == 409
        folder = main.WORK / job_id
        folder.mkdir(exist_ok=True)
        (folder/'media.mp4').write_bytes(b'fake mp4')
        main.set_job(job_id,status='completed',expires=main.epoch()+1000)
        assert c.get(f'/api/admin/jobs/{job_id}/file').content == b'fake mp4'
        assert c.get('/api/admin/jobs/invalid/file').status_code == 404
        removed = c.delete(f'/api/admin/jobs/{job_id}/files',headers={'X-Admin-CSRF':csrf})
        assert removed.status_code == 200,removed.text
        assert not folder.exists()
        assert c.get('/api/admin/storage').status_code == 200
        with main.db() as db:
            db.execute('UPDATE jobs SET created=? WHERE id=?', (main.epoch()-86400*8, job_id))
        main.admin_privacy_cleanup()
        assert next(r for r in c.get('/api/admin/jobs').json()['items'] if r['id']==job_id)['client_ip'] is None
        assert c.get('/api/history').json().get('items') is not None


def test_split_links_and_multiurl_batch(monkeypatch):
    from app.batch import split_links
    one='https://www.youtube.com/watch?v=aqz-KE-bpKQ'
    two='https://vimeo.com/12345'
    assert split_links(f'{one}, {two};\n {one}\t{two}') == [one,two]
    monkeypatch.setattr(main,'inspect_media',fake_info)
    main.init_db()
    main.rate.clear()
    with main.db() as c:
        c.execute("UPDATE jobs SET status='failed' WHERE status IN ('queued','running')")
    monkeypatch.setattr(main,'runner_started',True)
    with TestClient(main.app) as client:
        inspected=client.post('/api/inspect',json={'url':f'{one};\n{two}'}).json()
        assert inspected['kind']=='batch' and inspected['count']==2
        assert client.post('/api/downloads',json={
            'inspectionId':inspected['inspectionId'],'profileId':'video-mp4-720',
            'selectedPositions':[1,1],'concurrency':1}).status_code==422
        assert client.post('/api/downloads',json={
            'inspectionId':inspected['inspectionId'],'profileId':'video-mp4-720',
            'selectedPositions':[3],'concurrency':1}).status_code==422
        bad=client.post('/api/downloads',json={
            'inspectionId':inspected['inspectionId'],'profileId':'video-mp4-720',
            'selectedPositions':[1],'concurrency':main.MAX_PARALLEL_DOWNLOADS+1})
        assert bad.status_code in (422,)


def test_batch_individual_files_and_concurrency(monkeypatch):
    one='https://www.youtube.com/watch?v=aqz-KE-bpKQ'
    two='https://vimeo.com/12345'
    monkeypatch.setattr(main,'inspect_media',fake_info)
    monkeypatch.setattr(main,'runner_started',True)
    monkeypatch.setattr(main,'MAX_PARALLEL_DOWNLOADS',2)
    main.init_db()
    main.rate.clear()
    with main.db() as c:
        c.execute("UPDATE jobs SET status='failed' WHERE status IN ('queued','running')")
    with TestClient(main.app) as client:
        inspection=client.post('/api/inspect',json={'url':f'{one} {two}'}).json()
        result=client.post('/api/downloads',json={'inspectionId':inspection['inspectionId'],
            'profileId':'video-mp4-720','selectedPositions':[1,2], 'concurrency':1})
        assert result.status_code==202,result.text
        group=result.json()
        with main.db() as c:
            a=main.claim_next(main,c)
        assert a and a['parent_id']==group['id']
        with main.db() as c:
            assert main.claim_next(main,c) is None  # group permits 1 simultaneous
        # Fake yt-dlp emits final file. No network or real third-party media needed.
        class Proc:
            pid=999999
            def __init__(self,command,**kwargs):
                Path(kwargs['cwd'],'media.mp4').write_bytes(b'fake video 123')
                self.stdout=iter(['[download] DL:100.0%|12|12|NA'])
            def poll(self): return 0
            def wait(self): return 0
        monkeypatch.setattr(main.subprocess,'Popen',Proc)
        main.execute_job(a)
        with main.db() as c:
            b=main.claim_next(main,c)
        assert b and b['id']!=a['id']
        main.execute_job(b)
        status=client.get(f'/api/downloads/{group["id"]}',params={'token':group['token']}).json()
        assert status['status']=='completed',status
        assert status['batch']['completed']==2
        assert 'fileName' in status['batch']['items'][0]
        for x in status['batch']['items']:
            assert client.get(f'/api/downloads/{x["id"]}/file',params={'token':'wrong'}).status_code==404
            r=client.get(f'/api/downloads/{x["id"]}/file',params={'token':group['token']})
            assert r.status_code==200,r.text
            assert r.content==b'fake video 123'
            assert r.headers['content-type'].startswith('video/mp4')
        assert not (main.WORK/group['id']/'media.zip').exists()


def test_batch_global_limit_and_cancel(monkeypatch):
    monkeypatch.setattr(main,'MAX_PARALLEL_DOWNLOADS',1)
    monkeypatch.setattr(main,'runner_started',True)
    monkeypatch.setattr(main,'inspect_media',fake_info)
    main.init_db()
    main.rate.clear()
    with main.db() as c:
        c.execute("UPDATE jobs SET status='failed' WHERE status IN ('queued','running')")
    with TestClient(main.app) as client:
        inspection=client.post('/api/inspect',json={'url':'https://vimeo.com/12345,https://vimeo.com/45678'}).json()
        rejected=client.post('/api/downloads',json={'inspectionId':inspection['inspectionId'],
            'profileId':'video-mp4-720','selectedPositions':[1,2],'concurrency':2})
        assert rejected.status_code==422
        created=client.post('/api/downloads',json={'inspectionId':inspection['inspectionId'],
            'profileId':'video-mp4-720','selectedPositions':[1,2],'concurrency':1}).json()
        with main.db() as conn:
            claimed=main.claim_next(main,conn)
        assert claimed is not None
        with main.db() as conn:
            assert main.claim_next(main,conn) is None
        response=client.post(f'/api/downloads/{created["id"]}/cancel',params={'token':created['token']})
        assert response.status_code==200
        with main.db() as conn:
            canceled=conn.execute("SELECT count(*) n FROM jobs WHERE parent_id=? AND status='canceled'", (created['id'],)).fetchone()['n']
        assert canceled==1
def test_admin_download_file_requires_authentication(monkeypatch):
    import uuid, hashlib, json
    from fastapi.testclient import TestClient
    monkeypatch.setenv('ADMIN_PASSWORD', 'admin-test-password-123456789')
    main.init_db()
    job_id = uuid.uuid4().hex
    folder = main.WORK / job_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'media.mp3').write_bytes(b'audio bytes')
    profile = {'id':'audio-mp3-192', 'kind':'audio', 'ext':'MP3', 'label':'192 kbps'}
    with main.db() as c:
        c.execute("""INSERT INTO jobs (id,token_hash,url,title,profile,metadata,publish,source,status,stage,
        percent,filename,created,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job_id,hashlib.sha256(b'secret').hexdigest(),'https://vimeo.com/12345',
         'Audio',json.dumps(profile),0,0,'Vimeo','completed','Concluído',100,'Audio.mp3',main.epoch(),'single'))
    with TestClient(main.app) as c:
        path=f'/api/admin/jobs/{job_id}/file'
        assert c.get(path).status_code == 401
        assert c.post('/api/admin/login',json={'password':'admin-test-password-123456789'}).status_code == 200
        response=c.get(path)
        assert response.status_code == 200
        assert response.content == b'audio bytes'
        assert 'Audio.mp3' in response.headers['content-disposition']
        assert 'no-store' in response.headers['cache-control']
        assert c.get('/api/admin/jobs/abcdef/file').status_code == 404
        (folder / 'media.mp3').unlink()
        assert c.get(path).status_code == 404
