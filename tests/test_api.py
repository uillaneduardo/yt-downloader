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
