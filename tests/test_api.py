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


def test_inspection_and_public_eligibility(monkeypatch):
    import json
    from types import SimpleNamespace
    info = {
        'title': 'Conteúdo público', 'uploader': 'Criador', 'duration': 120,
        'availability': 'public', 'formats': [
            {'height': 720, 'vcodec': 'avc1', 'acodec': 'none'},
            {'height': None, 'vcodec': 'none', 'acodec': 'mp4a'},
        ],
    }
    monkeypatch.setattr(main.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(
        returncode=0, stdout=json.dumps(info), stderr=''))
    result = main.inspect_media('https://www.youtube.com/watch?v=aqz-KE-bpKQ')
    assert result['title'] == 'Conteúdo público'
    assert result['publicEligible'] is True
    assert any(p['id'] == 'video-mp4-720' for p in result['profiles'])
    info['availability'] = 'unlisted'
    assert main.inspect_media('https://www.youtube.com/watch?v=aqz-KE-bpKQ')['publicEligible'] is False
    info['duration'] = 0
    try:
        main.inspect_media('https://www.youtube.com/watch?v=aqz-KE-bpKQ')
        assert False, 'Expected error'
    except Exception as exc:
        assert getattr(exc, 'status_code', 0) == 422


def test_worker_completion_file_and_public_history(monkeypatch):
    import hashlib
    import json
    import uuid

    main.init_db()
    job_id = uuid.uuid4().hex
    token = 'local-test-secret-token'
    profile = {'id': 'audio-mp3-192', 'kind': 'audio', 'ext': 'MP3', 'label': '192 kbps'}
    with main.db() as c:
        c.execute('''INSERT INTO jobs (id, token_hash, url, title, profile, metadata, publish,
            source, status, stage, percent, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (job_id, hashlib.sha256(token.encode()).hexdigest(),
             'https://www.youtube.com/watch?v=aqz-KE-bpKQ', 'Título de Teste',
             json.dumps(profile), 1, 1, 'YouTube', 'running', 'Obtendo arquivo', None, main.epoch()))

    class FakeProcess:
        pid = 999999

        def __init__(self, command, **kwargs):
            assert '--embed-metadata' in command and '--embed-thumbnail' in command
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
        job = c.execute('SELECT status, filename, percent FROM jobs WHERE id=?', (job_id,)).fetchone()
        public = c.execute('SELECT count(*) FROM public_history WHERE url=?',
                           ('https://www.youtube.com/watch?v=aqz-KE-bpKQ',)).fetchone()[0]
    assert job['status'] == 'completed'
    assert job['filename'] == 'Título de Teste.mp3'
    assert job['percent'] == 100
    assert public >= 1
    with TestClient(main.app) as client:
        assert client.get(f'/api/downloads/{job_id}/file', params={'token': 'wrong'}).status_code == 404
        r = client.get(f'/api/downloads/{job_id}/file', params={'token': token})
        assert r.status_code == 200
        assert r.content == b'ID3mock-audio-data'
        assert 'audio/mpeg' in r.headers['content-type']


def test_forwarded_client_ip_opt_in(monkeypatch):
    from starlette.requests import Request

    scope = {'type': 'http', 'method': 'GET', 'scheme': 'http', 'path': '/api/inspect',
             'headers': [(b'cf-connecting-ip', b'192.0.2.10')],
             'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000), 'query_string': b''}
    monkeypatch.delenv('TRUST_CF_IP', raising=False)
    base = main.client_key(Request(scope))
    monkeypatch.setenv('TRUST_CF_IP', '1')
    assert main.client_key(Request(scope)) != base
    scope['headers'] = [(b'cf-connecting-ip', b'invalid-ip')]
    assert main.client_key(Request(scope)) == base
