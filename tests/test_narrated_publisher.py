import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

def test_publish_uses_selected_metadata_and_prevents_duplicates(tmp_path, monkeypatch):
    spec=importlib.util.spec_from_file_location('narrated_publish',Path('scripts/narrated_publish.py'))
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    monkeypatch.setattr(mod,'ROOT',tmp_path)
    token=tmp_path/'data/youtube/token.json';token.parent.mkdir(parents=True);token.write_text('{}')
    job=tmp_path/'job';job.mkdir();(job/'recap.mp4').write_bytes(b'test')
    options={'title':'Test story','description':'Test summary','privacy':'private','made_for_kids':False}
    (job/'request.json').write_text(json.dumps({'publication':options}))
    monkeypatch.setattr(mod.Credentials,'from_authorized_user_info',lambda _:SimpleNamespace(valid=True))
    monkeypatch.setattr(mod,'AuthorizedHttp',lambda *a,**k:None)
    monkeypatch.setattr(mod,'MediaFileUpload',lambda *a,**k:None)
    request=SimpleNamespace(next_chunk=lambda **kwargs:(None,{'id':'abcdefghijk'}))
    insert=Mock(return_value=request)
    service=SimpleNamespace(videos=lambda:SimpleNamespace(insert=insert),close=lambda:None)
    monkeypatch.setattr(mod,'build',lambda *a,**k:service)
    mod.upload(job)
    assert json.loads((job/'publication.json').read_text())['status']=='uploaded'
    assert insert.call_args.kwargs['body']['status']['privacyStatus']=='private'
    mod.upload(job)
    assert insert.call_count==1


def test_reconcile_processing_video_by_size_without_reupload(tmp_path, monkeypatch):
    import time
    from datetime import datetime, timezone
    spec=importlib.util.spec_from_file_location('narrated_publish_check',Path('scripts/narrated_publish.py'))
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    (tmp_path/'recap.mp4').write_bytes(b'12345')
    (tmp_path/'status.json').write_text(json.dumps({'duration':45.86}))
    def response(data):return SimpleNamespace(execute=lambda:data)
    video={'id':'abcdefghijk','snippet':{'title':'Story','description':'Recap\n\n#Shorts','publishedAt':datetime.now(timezone.utc).isoformat()},
        'contentDetails':{'duration':'P0D'},'fileDetails':{'fileSize':'5'}}
    y=SimpleNamespace(
        channels=lambda:SimpleNamespace(list=lambda **k:response({'items':[{'contentDetails':{'relatedPlaylists':{'uploads':'test'}}}]})),
        playlistItems=lambda:SimpleNamespace(list=lambda **k:response({'items':[{'snippet':{'title':'Story','resourceId':{'videoId':'abcdefghijk'}}}]})),
        videos=lambda:SimpleNamespace(list=lambda **k:response({'items':[video]})))
    assert mod.find_existing(y,{'title':'Story','description':'Recap'},tmp_path,time.time()-60)['id']=='abcdefghijk'
    video['fileDetails']['fileSize']='6'
    assert mod.find_existing(y,{'title':'Story','description':'Recap'},tmp_path,time.time()-60) is None
