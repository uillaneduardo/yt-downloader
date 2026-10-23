"""Regression tests for per-item profiles in multi-link batches."""
import os
import tempfile
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="yt-batch-cards-"))

from fastapi.testclient import TestClient
from app import main


def test_independent_profile_selection_and_rejections(monkeypatch):
    def inspect(url):
        profiles = ([{'id':'video-mp4-1080','kind':'video','label':'1080p','ext':'MP4','height':1080},
                     {'id':'audio-mp3-192','kind':'audio','label':'192 kbps','ext':'MP3'}]
                    if 'youtube.com' in url else
                    [{'id':'audio-mp3-192','kind':'audio','label':'192 kbps','ext':'MP3'}])
        return {'title':'Vídeo', 'author':'Canal','duration':120,
                'thumbnail':'','profiles':profiles,'publicEligible':True}
    monkeypatch.setattr(main, 'inspect_media', inspect)
    monkeypatch.setattr(main, 'runner_started', True)
    main.init_db()
    main.rate.clear()
    with main.db() as c:
        c.execute("UPDATE jobs SET status='failed' WHERE status IN ('queued','running')")
    with TestClient(main.app) as client:
        inspection=client.post('/api/inspect',json={
            'url':'https://youtu.be/aqz-KE-bpKQ; https://vimeo.com/12345'})
        assert inspection.status_code==200, inspection.text
        data=inspection.json()
        assert len(data['entries'])==2
        assert [len(entry['profiles']) for entry in data['entries']]==[2,1]
        body={'inspectionId':data['inspectionId'],'profileId':'video-mp4-1080',
              'selectedPositions':[1,2],'itemProfiles':{'1':'video-mp4-1080','2':'audio-mp3-192'},
              'concurrency':1}
        result=client.post('/api/downloads',json=body)
        assert result.status_code==202,result.text
        job=result.json()
        status=client.get('/api/downloads/'+job['id'],params={'token':job['token']}).json()
        assert [x['profile']['id'] for x in status['batch']['items']]==[
            'video-mp4-1080','audio-mp3-192']
        assert client.post('/api/downloads',json={**body,
            'selectedPositions':[2],'itemProfiles':{'2':'video-mp4-1080'}}).status_code==422
        assert client.post('/api/downloads',json={**body,
            'selectedPositions':[1],'itemProfiles':{'2':'audio-mp3-192'}}).status_code==422
        assert client.post('/api/downloads',json={**body,
            'itemProfiles':{'1':'fake','2':'audio-mp3-192'}}).status_code==422
        assert client.post('/api/downloads/'+job['id']+'/cancel',
                           params={'token':job['token']}).status_code==200


def test_legacy_global_profile_still_supported(monkeypatch):
    monkeypatch.setattr(main,'runner_started',True)
    monkeypatch.setattr(main,'inspect_media',lambda url:{
        'title':'A','duration':60,'author':'','thumbnail':'','publicEligible':True,
        'profiles':[{'id':'audio-mp3-128','kind':'audio','ext':'MP3','label':'128 kbps'}]})
    main.rate.clear()
    with main.db() as c:
        c.execute("UPDATE jobs SET status='failed' WHERE status IN ('queued','running')")
    with TestClient(main.app) as client:
        data=client.post('/api/inspect',json={
          'url':'https://vimeo.com/11111,https://vimeo.com/22222'}).json()
        r=client.post('/api/downloads',json={
          'inspectionId':data['inspectionId'],'profileId':'audio-mp3-128',
          'selectedPositions':[1,2], 'concurrency':1})
        assert r.status_code==202,r.text
        j=r.json()
        assert client.post('/api/downloads/'+j['id']+'/cancel',
                           params={'token':j['token']}).status_code==200
