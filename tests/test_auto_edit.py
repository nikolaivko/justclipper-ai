import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import config
from app.main import app
from app.models.schemas import (ClipCandidate, EDLSegment, EditorialScores, GeminiAnalysisResponse,
                                MicroBeat, ProtectedVisualRegion, SemanticVisualTarget)
from app.services.auto_edit_director import AutoEditDirector
from app.services.caption_director import CaptionDirector
from app.services.gemini_service import GeminiService
from app.services.job_store import job_store
from app.services.clip_service import ClipService
from app.services.worker import PipelineWorker


def story(**changes):
    data = dict(rank=1, title='The guest reveals why he left', short_description='The guest explains his decision.',
                topic='Decision', content_type='INTERVIEW', reason='Complete question and answer',
                categories=['STORY'], context_dependency='LOW',
                source_segments=[EDLSegment(start_seconds=10, end_seconds=26)],
                payoff_present=True, payoff_timestamp=24,
                scores=EditorialScores(first_1s_strength=8, first_3s_strength=8,
                                       standalone_clarity=8, payoff_strength=8, ending_strength=8))
    data.update(changes)
    return ClipCandidate(**data)


# Ten content fixtures: selection stays semantic; styles and framing vary.
@pytest.mark.parametrize('content,mode,style', [
    ('INTERVIEW', 'CONVERSATIONAL', 'PODCAST_DYNAMIC'),
    ('PODCAST', 'COMEDIC', 'COMEDY_TIMING'),
    ('EMOTIONAL_STORY', 'EMOTIONAL', 'EMOTIONAL_MINIMAL'),
    ('TUTORIAL', 'EDUCATIONAL', 'EDUCATIONAL_FAST'),
    ('PRODUCT_DEMO', 'VISUAL', 'PRODUCT_DEMO'),
    ('GAMEPLAY', 'ENERGETIC', 'GAMEPLAY_DYNAMIC'),
    ('SPORT', 'FAST', 'ACTION_ENERGY'),
    ('SCREEN_RECORDING', 'EDUCATIONAL', 'EDUCATIONAL_FAST'),
    ('VLOG', 'ENERGETIC', 'CLEAN_VIRAL'),
    ('STORY', 'STORY', 'STORY_DRIVEN'),
])
def test_auto_edit_content_fixtures(content, mode, style):
    clip = story(content_type=content, pacing_mode=mode)
    analysis = GeminiAnalysisResponse(source_summary='One complete source story.', clips=[clip])
    selected, _, _, _ = AutoEditDirector.select(analysis, 60, {})
    assert len(selected) == 1  # Never fills the configured ceiling with excerpts.
    shots, plan = AutoEditDirector.plan(selected[0], 60, {'tracks': [], 'speech': [], 'scenes': []})
    assert plan['style_profile'] == style
    assert plan['effect_events'] == []
    assert shots[-1]['output_end'] == 16


def test_auto_edit_quality_floor_and_duplicates():
    strong = story()
    duplicate = story(rank=2)
    weak = story(rank=3, source_segments=[EDLSegment(start_seconds=35, end_seconds=45)], payoff_timestamp=43,
                 scores=EditorialScores(first_1s_strength=1, payoff_strength=1, standalone_clarity=2))
    selected, audit, _, decisions = AutoEditDirector.select(GeminiAnalysisResponse(source_summary='One moment.', clips=[strong, duplicate, weak]), 60, {})
    assert len(selected) == 1
    assert 'DUPLICATE' in audit
    # Weak candidate was initially SELECTED but dropped by AutoEditDirector because of quality floor in AUTO mode
    assert any(d.get("decision") == "NOT_SELECTED" for d in decisions)


def test_hierarchical_analysis_applies_deep_story_and_calls_one_critic(monkeypatch):
    service = GeminiService()
    broad = story()
    deep = story(source_segments=[EDLSegment(start_seconds=10, end_seconds=14),
                                  EDLSegment(start_seconds=19, end_seconds=26)],
                 caption_emphasis=['left'], preserve_pauses=[EDLSegment(start_seconds=23, end_seconds=24)],
                 micro_beats=[MicroBeat(start_seconds=24, end_seconds=26, kind='PAYOFF', evidence='Answer concludes')])
    source_map = {'summary':'An interview', 'people':['guest','host'], 'topics':['decision'],
                  'windows':[{'start':10,'end':26,'story':'The reason for leaving', 'visual_evidence':'Guest and host', 'audio_evidence':'Question and answer'}]}
    responses = [source_map, GeminiAnalysisResponse(source_summary='An interview', clips=[broad]).model_dump(exclude_unset=True),
                 GeminiAnalysisResponse(source_summary='The answer', clips=[deep]).model_dump(exclude_unset=True), {'reviews':[]}]
    generate = Mock(side_effect=[SimpleNamespace(text=json.dumps(r)) for r in responses])
    service.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    result = service.analyze_video(object(), {**AutoEditDirector.settings().model_dump(), 'source_duration':60})
    assert generate.call_count == 4
    assert result.source_map == source_map
    assert result.clips[0].source_segments == deep.source_segments
    assert result.clips[0].caption_emphasis == ['left']
    assert result.clips[0].deep_analysis_status == 'COMPLETE'
    chosen, _, _, _ = AutoEditDirector.select(result, 60, {})
    _, plan = AutoEditDirector.plan(chosen[0], 60, {})
    assert plan['retention_map'][0]['start'] == 9  # 4s setup + source 24-19.
    assert plan['retention_map'][0]['end'] == 11
    assert plan['retention_map'][0]['label'] == 'PAYOFF'


def test_deep_failure_keeps_broad_story(monkeypatch):
    service = GeminiService()
    analysis = GeminiAnalysisResponse(source_summary='An interview', clips=[story()])
    generate = Mock(side_effect=[RuntimeError('provider unavailable'), SimpleNamespace(text='{"reviews":[]}')])
    service.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    service._deepen_and_critique(object(), analysis, {}, 'configured-model', 60)
    assert analysis.clips[0].source_segments == story().source_segments
    assert analysis.clips[0].deep_analysis_status == 'BROAD_FALLBACK'
    assert generate.call_count == 2


def test_caption_plan_avoids_important_product_and_remaps_cuts():
    clip = story(caption_emphasis=['result'], protected_regions=[ProtectedVisualRegion(
        start_seconds=10, end_seconds=26, x=.1, y=.5, width=.8, height=.45)],
        source_segments=[EDLSegment(start_seconds=10, end_seconds=14), EDLSegment(start_seconds=19, end_seconds=26)])
    shots, _ = AutoEditDirector.plan(clip, 60, {})
    captions, plan = CaptionDirector.plan(clip, [{'start_seconds':20,'end_seconds':22,'text':'The result is clear'}], shots, [])
    assert captions[0]['emphasis'] == ['result']
    assert plan['unit_count'] == 1
    assert all(s['caption_y'] == .12 for s in shots)
    assert all(not p['overlap_risk'] for p in plan['positions'])


@pytest.mark.parametrize('source_flow', ['upload', 'pasted_url'])
def test_make_shorts_queues_and_renders_real_mp4_with_truthful_caption_fallback(tmp_path, monkeypatch, source_flow):
    from app.api import routes
    from app.services.ffmpeg_service import FFmpegService
    from app.services.video_analyzer import LocalVideoAnalyzer
    from app.services.storage_lifecycle import storage_lifecycle
    from app.services.transcription_service import TranscriptionService
    from app.services.youtube_publisher import youtube_publisher
    from app.services.semantic_cache import SemanticCache
    output, jobs = tmp_path/'output', tmp_path/'jobs'
    output.mkdir(); jobs.mkdir()
    source = tmp_path/'source.mp4'
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=30',
                    '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','6',
                    '-c:v','libx264','-c:a','aac',str(source)], check=True, capture_output=True)
    monkeypatch.setattr(job_store, 'jobs_dir', jobs)
    monkeypatch.setattr(config, 'OUTPUT_DIR', output)
    monkeypatch.setattr(config, 'DATA_CACHE', tmp_path/'cache')
    monkeypatch.setattr(storage_lifecycle, 'root', tmp_path)
    monkeypatch.setattr(storage_lifecycle, 'require_processing_space', Mock())
    monkeypatch.setattr(storage_lifecycle, 'cleanup_source', Mock())
    monkeypatch.setattr(storage_lifecycle, 'cleanup_job_intermediates', Mock())
    monkeypatch.setattr('app.services.disk_cleaner.free_disk_space', Mock())
    monkeypatch.setattr(LocalVideoAnalyzer, 'detect_silences', Mock(return_value=[]))
    monkeypatch.setattr(LocalVideoAnalyzer, 'detect_scenes', Mock(return_value=[]))
    monkeypatch.setattr(LocalVideoAnalyzer, 'detect_speech', Mock(return_value=[]))
    monkeypatch.setattr(LocalVideoAnalyzer, 'track_faces', Mock(side_effect=RuntimeError('tracking unavailable')))
    monkeypatch.setattr(TranscriptionService, 'transcribe_segment', Mock(side_effect=RuntimeError('caption provider unavailable')))
    monkeypatch.setattr(GeminiService, 'upload_video', Mock(return_value=SimpleNamespace(name='fixture')))
    monkeypatch.setattr(GeminiService, 'delete_video', Mock())
    result = GeminiAnalysisResponse(source_summary='One story.', clips=[story(
        source_segments=[EDLSegment(start_seconds=0, end_seconds=5.5)], payoff_timestamp=5)])
    monkeypatch.setattr(GeminiService, 'analyze_video', Mock(return_value=result))
    publish = Mock(); monkeypatch.setattr(youtube_publisher, 'publish_video', publish)
    if source_flow == 'pasted_url':
        from app.services.download_service import DownloadService
        from app.services.source_store import source_store
        import os
        service = DownloadService(tmp_path)
        monkeypatch.setattr(routes, 'download_service', service)
        (tmp_path/'registry').mkdir(exist_ok=True)
        monkeypatch.setattr(source_store, 'registry_dir', tmp_path/'registry')
        monkeypatch.setattr(routes.youtube_service, 'get_video_rights', Mock(return_value={
            'rights_status':'UNKNOWN','title':'Public creator','privacy_status':'public','upload_status':'processed'}))
        target = service.trends_dir/'abcdefghijk'/'source.mp4'
        target.parent.mkdir(parents=True)
        os.link(source, target)
        response = TestClient(app).post('/api/download/youtube', json={
            'url':'https://youtu.be/abcdefghijk?si=share','auto_edit':True,'count_mode':'TARGET','clip_count':5})
        assert response.status_code == 200
        job_id = service.get_status(response.json()['dl_id'])['job_id']
        queued = job_store.get(job_id)
        assert queued['auto_render'] is False
        assert queued['analysis_settings']['target_clip_count'] == 5
        assert source_store.get('youtube_abcdefghijk').requested_clip_count == 5
        PipelineWorker()._process_job(queued)
        finished = job_store.get(job_id)
        assert finished['status'] == 'READY_TO_RENDER'
        assert len(finished['candidates']) == 1
        assert finished['target_metrics']['requested_clip_count'] == 5
        assert finished['target_metrics']['final_clip_count'] == 1
        assert not finished.get('renders')
        service.stop()
        publish.assert_not_called()
        return
    job = ClipService.create_job('auto', 'source.mp4', source)
    job['metadata'] = FFmpegService.get_metadata(source).model_dump()
    job_store.set('auto', job)
    response = TestClient(app).post('/api/auto-edit/auto', json={})
    assert response.status_code == 200
    queued = job_store.get('auto')
    assert queued['auto_render'] is True
    assert queued['analysis_settings']['exact_clip_count'] is False
    PipelineWorker()._process_job(queued)
    finished = job_store.get('auto')
    assert finished['status'] == 'COMPLETED'
    assert len(finished['renders']) == 1
    render = finished['renders'][0]
    assert render['warnings'] == ['CAPTIONS_UNAVAILABLE']
    assert render['edit_plan']['caption_plan']['status'] == 'UNAVAILABLE'
    from pathlib import Path
    meta = FFmpegService.get_metadata(Path(render['output_path']))
    assert (meta.width, meta.height) == (1080,1920)
    assert meta.video_codec == 'h264' and meta.audio_codec == 'aac'
    assert 5.4 <= meta.duration <= 5.7
    assert TranscriptionService._model is None
    publish.assert_not_called()


def test_auto_edit_settings_ignore_manual_count_and_caption_preferences():
    settings = AutoEditDirector.settings('Keep educational explanations')
    assert settings.auto_edit and not settings.exact_clip_count
    assert settings.subtitles and settings.output_resolution == '1080x1920'
    assert settings.min_clip_seconds == 5
    assert settings.editorial_guidance == 'Keep educational explanations'


def test_make_shorts_ui_uses_auto_endpoint_and_waits_for_results():
    from pathlib import Path
    script = r"""
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const nodes={};
const doc={addEventListener(){},getElementById(id){return nodes[id] ||= {style:{},value:'',disabled:false};}};
let url, body, target;
const context=vm.createContext({window:{}, document:doc,console,fetch:async(u,options)=>{
  url=u; body=JSON.parse(options.body); return {ok:true};}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
vm.runInContext("currentJobId='sample'; showAnalysisStatus=()=>{}; pollJobStatus=(status)=>{ capturedStatus=status; };",context);
(async()=>{
 await vm.runInContext('startAutoEdit()',context);
 assert.equal(url,'/api/auto-edit/sample');
 assert.equal(body.editorial_guidance,null);
 assert.equal(body.target_clip_count,null);
 assert.equal(body.count_mode,'AUTO');
 assert.equal(vm.runInContext('capturedStatus',context),'COMPLETED');
 assert.equal(nodes['btn-make-shorts'].disabled,false);
})().catch(e=>{console.error(e);process.exit(1)});
"""
    subprocess.run(['node','-e',script,str(Path(__file__).parents[1]/'app/ui/app.js')], check=True, capture_output=True)


def test_caption_style_does_not_invalidate_auto_semantic_cache(tmp_path):
    from app.services.semantic_cache import SemanticCache
    source = tmp_path/'source.mp4'; source.write_bytes(b'fixture-source-hash')
    settings = AutoEditDirector.settings().model_dump()
    cache = SemanticCache(tmp_path/'cache')
    assert cache.key(source, settings) == cache.key(source, {**settings, 'subtitles':False, 'output_resolution':'720x1280'})
    assert cache.key(source, settings) != cache.key(source, {**settings, 'auto_edit':False})


def test_auto_edit_rejects_an_unconfirmed_payoff():
    clip = story(payoff_present=None, payoff_timestamp=None)
    selected, audit, _, decisions = AutoEditDirector.select(GeminiAnalysisResponse(source_summary='Uncertain.', clips=[clip]), 60, {})
    assert selected == []
    assert 'PAYOFF_MISSING' in audit


def test_source_map_failure_keeps_the_existing_video_discovery_path():
    service = GeminiService()
    service.client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=[
        RuntimeError('map unavailable'),
        SimpleNamespace(text=GeminiAnalysisResponse(source_summary='A story', clips=[story()]).model_dump_json(exclude_unset=True)),
        RuntimeError('deep unavailable'),
        SimpleNamespace(text='{"reviews":[]}')
    ])))
    result = service.analyze_video(object(), {**AutoEditDirector.settings().model_dump(), 'source_duration':60})
    assert len(result.clips) == 1
    assert result.analysis_warnings == ['SOURCE_MAP_UNAVAILABLE']
    assert result.clips[0].deep_analysis_status == 'BROAD_FALLBACK'
    assert len(AutoEditDirector.select(result, 60, {})[0]) == 1


def test_captions_do_not_collide_with_context_overlay():
    from app.models.schemas import ContextOverlay
    clip = story(context_overlay=ContextOverlay(text='Final question', evidence_event_id='question', start_seconds=10, end_seconds=14),
                 protected_regions=[ProtectedVisualRegion(start_seconds=10, end_seconds=26, x=0, y=.5, width=1, height=.45)])
    shots, _ = AutoEditDirector.plan(clip, 60, {})
    _, plan = CaptionDirector.plan(clip, [{'start_seconds':11, 'end_seconds':13, 'text':'The answer is clear'}], shots, [])
    assert shots[0]['caption_y'] == .35
    assert not plan['positions'][0]['overlap_risk']
