import json
import math
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app.narrated.core import Plan, Shot, fit_shots, probe, run, ROOT, write_json


def sample_plan():
    text = 'Watch the colored scene change as the camera shows another clear view of the same simple test pattern.'
    return Plan(title='A colorful test story', summary='A synthetic test of scene timing and captions.', language='en', scenes=[
        dict(kind='narration', text=text, shots=[dict(start=0, end=12, evidence='A colored synthetic test frame')]) for _ in range(4)])


def test_plan_schema_serializes_through_gemini_sdk():
    from google import genai
    from google.genai import _transformers
    client = genai.Client(api_key='offline-test-key')
    try:
        schema = _transformers.t_schema(client._api_client, Plan)
        assert schema.properties['scenes'].items.properties['shots'].items.properties['end'].minimum == 0
    finally:
        client.close()
    from app.narrated.core import gemini_plan_schema
    from google.genai import types
    wire = gemini_plan_schema()
    types.Schema.model_validate(wire)
    assert not any(key in json.dumps(wire) for key in ['exclusiveMinimum', 'minimum', 'minLength', '$ref'])
    assert wire['properties']['language']['enum'] == ['en']
    for end in [0, -1, .1, float('nan'), float('inf')]:
        with pytest.raises(ValueError):
            Shot(start=0, end=end, evidence='Visible action')


def test_plan_rejects_out_of_range_and_bad_speech_fit():
    plan = sample_plan()
    plan.check_source(12)
    with pytest.raises(ValueError):
        plan.check_source(5)
    shots = [Shot(start=3, end=8, evidence='Visible action'), Shot(start=20, end=25, evidence='Visible reaction')]
    assert fit_shots(shots, 6) == [(3, 3), (20, 3)]
    for duration in [11, 0, math.nan]:
        with pytest.raises(ValueError):
            fit_shots(shots, duration)


def test_short_speech_is_paced_within_available_footage():
    from app.narrated.core import Scene, narration_durations
    durations = [4.72, 5.28, 4.4, 4.48, 4.48, 4.72, 5.44]
    available = [6, 6, 7, 7, 8, 8, 8]
    scenes = [Scene(kind='narration', text='Test sentence.', shots=[Shot(start=0,end=n,evidence='Visible action')]) for n in available]
    paced = narration_durations(scenes, durations)
    assert sum(paced) == pytest.approx(40.5)
    assert all(.8-1e-8 <= old/new <= 1.2 for old,new in zip(durations,paced))
    assert all(new <= limit for new,limit in zip(paced,available))
    assert narration_durations(scenes, durations, allow_slowdown=False) == durations
    with pytest.raises(ValueError, match='natural pace'):
        narration_durations(scenes, [1]*7)


def test_plan_repairs_bad_timestamps_without_another_upload(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from google import genai
    from app.config import config
    from app.narrated import pipeline
    valid = sample_plan().model_dump()
    invalid = json.loads(json.dumps(valid))
    invalid['scenes'][0]['shots'][0].update(start=30, end=42)
    replies = iter([invalid, valid])
    calls = []
    remote = SimpleNamespace(name='files/test', uri='test://video', state=SimpleNamespace(name='ACTIVE'))
    def generate(**kwargs):
        calls.append(('generate', kwargs['contents'][1]))
        return SimpleNamespace(text=json.dumps(next(replies)))
    client = SimpleNamespace(
        files=SimpleNamespace(upload=lambda **kw: (calls.append(('upload', kw)) or remote),
                              delete=lambda **kw: calls.append(('delete', kw))),
        models=SimpleNamespace(generate_content=generate), close=lambda: None)
    monkeypatch.setattr(genai, 'Client', lambda **kwargs: client)
    monkeypatch.setattr(config, 'GEMINI_API_KEY', 'offline-test-key')
    monkeypatch.setattr(pipeline, 'probe', lambda path: (12, [{'codec_type':'video'}]))
    monkeypatch.setattr(pipeline, 'run', lambda *args, **kwargs: '')
    write_json(tmp_path/'request.json', {'guidance':''})
    pipeline.create_plan(tmp_path)
    assert [c[0] for c in calls] == ['upload', 'generate', 'generate', 'delete']
    assert 'exceeds source duration' in calls[2][1]
    assert json.loads((tmp_path/'status.json').read_text())['stage'] == 'review'
    Plan.model_validate_json((tmp_path/'plan.json').read_text()).check_source(12)


def test_api_isolation_cleanup_and_origin(tmp_path, monkeypatch):
    from app import narrated_main as api
    jobs, voices = tmp_path/'jobs', tmp_path/'voices'
    monkeypatch.setattr(api, 'JOBS', jobs)
    monkeypatch.setattr(api, 'VOICES', voices)
    with TestClient(api.app) as client:
        assert client.get('/').status_code == 200
        assert client.get('/api/voices').json() == []
        assert client.post('/api/jobs', headers={'Origin':'https://evil.example'}).status_code == 403
        assert client.get('/api/jobs/../../anything').status_code == 404
        folder = jobs/('a'*32)
        folder.mkdir()
        write_json(folder/'status.json', {'stage':'review', 'progress':35})
        assert client.get('/api/jobs/'+'a'*32).json()['stage'] == 'review'
        assert client.delete('/api/jobs/'+'a'*32).status_code == 200
        assert not folder.exists()
        # Closing an already-cleaned project must also unblock the browser.
        assert client.delete('/api/jobs/'+'a'*32).status_code == 200
        profile = voices/('b'*32)
        profile.mkdir()
        write_json(profile/'profile.json', {'name':'Saved voice'})
        assert client.get('/api/voices').json()[0]['name'] == 'Saved voice'
    assert profile.exists()  # Explicitly saved profiles survive project close.


def test_no_generic_voice_fallback(tmp_path):
    from types import SimpleNamespace
    from app.narrated.voice import Voice
    sample=tmp_path/'sample.wav'
    sample.touch()
    def fail(*args, **kwargs):
        assert kwargs['ref_audio'] == str(sample)
        raise RuntimeError('Reference conditioning failed')
    voice = Voice(sample, 'This is a reference transcript')
    voice.backend = SimpleNamespace(model=SimpleNamespace(generate=fail))
    with pytest.raises(RuntimeError, match='Reference conditioning failed'):
        voice.speak('Hello', tmp_path/'out.wav')
    assert not (tmp_path/'out.wav').exists()


def test_voice_load_keeps_mlx_on_inference_thread(tmp_path, monkeypatch):
    import asyncio
    import sys
    import threading
    from types import SimpleNamespace
    from app.narrated.voice import Voice
    sample = tmp_path/'sample.wav'
    sample.touch()
    calls = []
    class Backend:
        def __init__(self, model_size): pass
        def _load_model_sync(self, size):
            calls.append(threading.get_ident())
            self.model = SimpleNamespace(generate=lambda text, ref_audio: None)
        async def load_model_async(self):
            raise AssertionError('Do not move MLX loading into an executor')
    monkeypatch.setitem(sys.modules, 'backend', SimpleNamespace(config=SimpleNamespace(set_data_dir=lambda p: None)))
    monkeypatch.setitem(sys.modules, 'backend.backends.mlx_backend', SimpleNamespace(MLXTTSBackend=Backend))
    voice = Voice(sample, 'Reference speech')
    asyncio.run(voice.load())
    assert calls == [threading.get_ident()]


def test_real_ffmpeg_recap_and_cleanup(tmp_path, monkeypatch):
    """Actual media path; synthetic speech stub, no voice model or Gemini calls."""
    from app.narrated import pipeline, voice
    profiles = tmp_path/'voices'
    profile = profiles/('a'*32)
    profile.mkdir(parents=True)
    write_json(profile/'profile.json', {'transcript':'Synthetic test only'})
    (profile/'sample.wav').touch()
    monkeypatch.setattr(pipeline, 'VOICES', profiles)
    class SyntheticVoice:
        def __init__(self, *args): self.durations = iter([10.013, 9.879, 10.080, 10.090])
        async def load(self): pass
        def speak(self, text, output):
            duration = next(self.durations)
            run([ROOT/'bin/ffmpeg','-nostdin','-y','-f','lavfi','-i','sine=frequency=440:sample_rate=24000','-t',duration,output])
            return duration
    monkeypatch.setattr(voice, 'Voice', SyntheticVoice)
    monkeypatch.setattr(pipeline, 'caption_timings', lambda folder, scenes, source: [[] for _ in scenes])
    job = tmp_path/'job'
    job.mkdir()
    write_json(job/'request.json', {'voice_id':'a'*32})
    plan = sample_plan().model_dump()
    for scene in plan['scenes']:
        scene['shots'] = [dict(start=0,end=6,evidence='Synthetic first shot'), dict(start=6,end=12,evidence='Synthetic second shot')]
    write_json(job/'plan.json', plan)
    run([ROOT/'bin/ffmpeg','-nostdin','-y','-f','lavfi','-i','color=c=blue:s=320x180:r=30','-t','12','-c:v','libx264','-threads','2',job/'source.mp4'])
    pipeline.render(job)
    duration, streams = probe(job/'recap.mp4')
    assert 39.8 < duration < 40.8
    assert {s['codec_type'] for s in streams} == {'video','audio'}
    video = next(s for s in streams if s['codec_type']=='video')
    assert (video['width'],video['height']) == (720,1280)
    assert video['time_base'] == '1/90000'
    assert abs(float(video['duration'])-duration) < .2
    assert not (job/'source.mp4').exists()
    assert not list(job.glob('voice-*.wav'))
    assert json.loads((job/'status.json').read_text())['stage'] == 'complete'


def test_portrait_framing_and_justified_wide(tmp_path):
    from app.narrated.core import portrait_filter
    import subprocess
    for mode, reason in [('portrait', ''), ('wide', 'Keep both distant subjects visible')]:
        shot = Shot(start=0, end=1, evidence='Red landscape frame', framing=mode, framing_reason=reason)
        result = subprocess.run([str(ROOT/'bin/ffmpeg'), '-v', 'error', '-f', 'lavfi',
            '-i', 'color=red:s=320x180:r=1', '-filter_complex', portrait_filter(shot),
            '-map', '[v]', '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
            capture_output=True, check=True)
        pixels = result.stdout
        assert len(pixels) == 720*1280*3
        top = pixels[(20*720+360)*3:(20*720+360)*3+3]
        center = pixels[(640*720+360)*3:(640*720+360)*3+3]
        assert center[0] > 200
        assert (top[0] > 200) if mode == 'portrait' else (max(top) < 10)
    with pytest.raises(ValueError, match='specific visual reason'):
        Shot(start=0,end=1,evidence='Visible action',framing='wide')


def test_footage_repair_preserves_script_and_covers_real_speech():
    from app.narrated.pipeline import validate_footage_budget, validate_repaired_footage
    original = sample_plan()
    short = original.model_copy(deep=True)
    for scene in short.scenes:
        scene.shots[0].end = 2
    with pytest.raises(ValueError, match='matching footage'):
        validate_footage_budget(short)
    validate_repaired_footage(short, original, [10]*4)
    with pytest.raises(ValueError, match='matching footage'):
        validate_repaired_footage(original, short, [10]*4)
    edited = original.model_copy(deep=True)
    edited.scenes[0].text = 'Changed meaning.'
    with pytest.raises(ValueError, match='preserve'):
        validate_repaired_footage(original, edited, [10]*4)


def test_youtube_links_are_strict():
    from app.narrated.core import youtube_video_id, MAX_SOURCE_SECONDS
    assert MAX_SOURCE_SECONDS == 10800
    for url in ['https://youtu.be/abcdefghijk', 'https://www.youtube.com/watch?v=abcdefghijk&list=xx',
                'https://youtube.com/shorts/abcdefghijk']:
        assert youtube_video_id(url) == 'abcdefghijk'
    for url in ['https://youtube.com.evil.test/watch?v=abcdefghijk', 'file:///etc/passwd',
                'https://youtube.com/watch?v=bad', 'https://evil.test/abcdefghijk']:
        with pytest.raises(ValueError):
            youtube_video_id(url)


def test_youtube_job_starts_isolated_worker(tmp_path, monkeypatch):
    from app import narrated_main as api
    monkeypatch.setattr(api, 'JOBS', tmp_path)
    monkeypatch.setattr(api, 'idle', lambda: None)
    launched = []
    monkeypatch.setattr(api, 'launch', lambda *args: launched.append(args))
    result = api.new_youtube_job(api.YoutubeRequest(url='https://youtu.be/abcdefghijk', guidance='Focus on the ending'))
    request = json.loads((tmp_path/result['id']/'request.json').read_text())
    assert request['video_id'] == 'abcdefghijk'
    assert launched == [(result['id'], 'youtube')]


def test_gemini_busy_retries_and_falls_back(monkeypatch):
    from app.narrated.generation import generate
    from types import SimpleNamespace
    calls = []
    class Busy(Exception):
        code = 503
    def request(**kw):
        calls.append(kw['model'])
        if kw['model']=='first': raise Busy()
        return 'script'
    monkeypatch.setattr('app.narrated.generation.time.sleep', lambda seconds: None)
    config = SimpleNamespace(GEMINI_MODEL='first',gemini_models=lambda m:['first','second'])
    client = SimpleNamespace(models=SimpleNamespace(generate_content=request))
    assert generate(client,config,['video'],{'type':'OBJECT'},lambda msg:None)=='script'
    assert calls==['first','first','second']


def test_publish_blocks_duplicates_without_uploading(tmp_path,monkeypatch):
    from app import narrated_main as api
    monkeypatch.setattr(api,'JOBS',tmp_path)
    monkeypatch.setattr(api,'idle',lambda:None)
    folder=tmp_path/('f'*32);folder.mkdir()
    (folder/'recap.mp4').touch()
    write_json(folder/'request.json',{})
    launched=[]
    monkeypatch.setattr(api,'launch',lambda *a:launched.append(a))
    options=api.PublicationOptions(title='A real title',privacy='private')
    assert api.publish_job(folder.name,options)=={'started':True}
    assert launched==[(folder.name,'publish')]
    write_json(folder/'publication.json',{'status':'uploaded','video_id':'abcdefghijk'})
    with pytest.raises(Exception) as caught:
        api.publish_job(folder.name,options)
    assert caught.value.status_code==409


def test_estimated_speech_length_never_discards_valid_script(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from google import genai
    from app.config import config
    from app.narrated import pipeline
    draft = sample_plan().model_dump()
    for scene in draft['scenes']:
        scene['shots'][0]['end'] = 6.5
    remote = SimpleNamespace(name='files/test',uri='test://video',state=SimpleNamespace(name='ACTIVE'))
    client = SimpleNamespace(files=SimpleNamespace(upload=lambda **kw:remote,delete=lambda **kw:None),
        models=SimpleNamespace(generate_content=lambda **kw:SimpleNamespace(text=json.dumps(draft))),close=lambda:None)
    monkeypatch.setattr(genai,'Client',lambda **kw:client)
    monkeypatch.setattr(config,'GEMINI_API_KEY','offline')
    monkeypatch.setattr(pipeline,'probe',lambda p:(12,[{'codec_type':'video'}]))
    monkeypatch.setattr(pipeline,'run',lambda *a,**kw:'')
    write_json(tmp_path/'request.json',{'guidance':''})
    pipeline.create_plan(tmp_path)
    state=json.loads((tmp_path/'status.json').read_text())
    assert state['stage']=='review'
    saved=Plan.model_validate_json((tmp_path/'plan.json').read_text()).check_source(12)
    assert saved.scenes[0].shots[0].end==6.5
    # Actual audio safety is retained: genuinely overlong speech still cannot
    # be fitted by freezing/repeating footage.
    with pytest.raises(ValueError):
        fit_shots(saved.scenes[0].shots, 15)


def test_captions_use_short_groups_and_measured_words(tmp_path):
    from app.narrated.subtitles import write_captions, timed_words
    text = 'Tareq seeks refuge beneath a cafe trapdoor.'
    recognized = [{'word':w, 'start':i*.5+.2, 'end':i*.5+.5} for i,w in enumerate(text.split())]
    words = timed_words(text,4,recognized)
    assert words[0]['start']==.2
    assert words[1]['start']==.7
    path=tmp_path/'subs.ass'
    write_captions(path,text,4,recognized)
    events=[l for l in path.read_text().splitlines() if l.startswith('Dialogue:')]
    import re
    assert len(events)==7
    for event in events:
        visible=re.sub(r'{[^}]*}','',event.split(',,',2)[-1])
        assert len(visible.split())<=3
        assert '66D8FF' in event
