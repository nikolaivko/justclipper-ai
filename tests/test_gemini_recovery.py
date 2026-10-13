import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.config import config
from app.models.schemas import ClipCandidate, EDLSegment, EditorialScores, GeminiAnalysisResponse
from app.services.gemini_requests import GenerationSession, GenerationFailure, TransientGenerationFailure
from app.services.gemini_service import GeminiService
from app.services.auto_edit_director import AutoEditDirector
from app.services.worker import PipelineWorker
from app.services.job_store import job_store
from app.services.semantic_cache import SemanticCache

class ProviderError(Exception):
    def __init__(self,code):
        self.code=code
        super().__init__('provider unavailable')

@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr('app.services.gemini_requests.time.sleep',Mock())


def clip(end=45):
    return ClipCandidate(rank=1,title='A real story',short_description='Context and payoff',topic='Story',
        content_type='INTERVIEW',reason='Complete story',categories=[],context_dependency='LOW',
        source_segments=[EDLSegment(start_seconds=0,end_seconds=end)],payoff_present=True,payoff_timestamp=40,
        scores=EditorialScores(first_1s_strength=9,first_3s_strength=9,standalone_clarity=9,payoff_strength=9,ending_strength=9))


def setup_service(monkeypatch, generate, models=('a',)):
    service=GeminiService()
    service.client=SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=generate)))
    monkeypatch.setattr(service,'_model_chain',lambda _:list(models))
    return service


def test_deep_503_preserves_broad_and_marks_optional_phases_unavailable(monkeypatch):
    service=setup_service(monkeypatch,lambda **kw: (_ for _ in ()).throw(ProviderError(503)))
    result=GeminiAnalysisResponse(source_summary='Broad success',clips=[clip()])
    before=result.clips[0].model_dump()
    service._deepen_and_critique(object(),result,{},'a',200)
    candidate=result.clips[0]
    assert candidate.source_segments==clip().source_segments
    assert candidate.scores.model_dump()==before['scores']
    assert candidate.deep_pass_status=='UNAVAILABLE'
    assert candidate.deep_pass_error_code=='SERVICE_UNAVAILABLE'
    assert candidate.critic_status=='UNAVAILABLE' and candidate.critic_issue is None
    assert len(AutoEditDirector.select(result,200,{})[0])==1
    assert service.client.models.generate_content.call_count==3


def test_critic_503_preserves_successful_deep_candidate(monkeypatch):
    response=GeminiAnalysisResponse(source_summary='Deep',clips=[clip()]).model_dump_json(exclude_unset=True)
    def generate(**kw):
        if isinstance(kw['contents'],str):raise ProviderError(503)
        return SimpleNamespace(text=response)
    service=setup_service(monkeypatch,generate)
    result=GeminiAnalysisResponse(source_summary='Broad',clips=[clip()])
    service._deepen_and_critique(object(),result,{},'a',200)
    assert result.clips[0].deep_pass_status=='COMPLETE'
    assert result.clips[0].critic_status=='UNAVAILABLE'
    assert result.clips[0].critic_issue is None
    assert len(AutoEditDirector.select(result,200,{})[0])==1


def test_deep_fallback_model_success_is_recorded(monkeypatch):
    def generate(**kw):
        if kw['model']=='a':raise ProviderError(503)
        return SimpleNamespace(text='{"reviews":[]}' if isinstance(kw['contents'],str) else
            GeminiAnalysisResponse(source_summary='Deep',clips=[clip()]).model_dump_json(exclude_unset=True))
    service=setup_service(monkeypatch,generate,('a','b'))
    result=GeminiAnalysisResponse(source_summary='Broad',clips=[clip()])
    service._deepen_and_critique(object(),result,{},'a',200)
    assert result.clips[0].deep_pass_status=='COMPLETE'
    assert result.analysis_models['deep']=='b'
    history=service._generation_session.diagnostics
    assert any(d['phase']=='deep' and d['model']=='a' and d['http_status']==503 for d in history)
    assert any(d['phase']=='deep' and d['model']=='b' and d['category']=='SUCCESS' for d in history)


def test_all_configured_models_transient_returns_typed_diagnostics(monkeypatch):
    service=setup_service(monkeypatch,lambda **kw: (_ for _ in ()).throw(ProviderError(503)),('a','b'))
    with pytest.raises(TransientGenerationFailure) as error:
        service._generate_content(model='a',contents='broad',phase='broad')
    assert error.value.error_code=='AI_SERVICE_UNAVAILABLE'
    assert {d['model'] for d in error.value.diagnostics}=={'a','b'}
    assert max(d['attempt'] for d in error.value.diagnostics)==3


@pytest.mark.parametrize('failure',[ProviderError(429),ProviderError(502),ProviderError(504),ConnectionError(),TimeoutError()])
def test_transient_backoff_is_bounded_with_jitter(monkeypatch,failure):
    sleep=Mock();monkeypatch.setattr('app.services.gemini_requests.time.sleep',sleep)
    client=SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=failure)))
    with pytest.raises(TransientGenerationFailure):GenerationSession().generate(client,['a'],'text',phase='broad')
    assert client.models.generate_content.call_count==3
    waits=[c.args[0] for c in sleep.call_args_list]
    assert 5<=waits[0]<=6 and 10<=waits[1]<=11


def test_compression_503_preserves_original_without_quality_rejection():
    candidate=clip(142)
    result=GeminiAnalysisResponse(source_summary='Broad',clips=[candidate])
    service=Mock();service.compress_candidate.side_effect=GenerationFailure({'a':ProviderError(503)})
    original=candidate.source_segments[0].model_dump()
    AutoEditDirector.compress(service,object(),result,{'source_duration':200})
    assert candidate.compression_attempted and candidate.compression_status=='SERVICE_UNAVAILABLE'
    assert candidate.source_segments[0].model_dump()==original
    selected,_,_,decisions=AutoEditDirector.select(result,200,{})
    assert not selected and decisions[0]['decision']=='COMPRESSION_SERVICE_UNAVAILABLE'
    assert candidate.hard_rejection_reason is None
    assert not candidate.quality_flags


@pytest.mark.parametrize('segments,expected',[
    ([(0,20),(35,60)],'SUCCESS'),
    ([(-1,50)],'FAILED'),
    ([(0,20),(19,45)],'FAILED'),
    ([(0,61)],'FAILED'),
    ([(10,35)],'FAILED'), # payoff removed
    ([(0,20),(135,150)],'FAILED'),
])
def test_compression_validates_before_promoting(segments,expected):
    candidate=clip(142);original=candidate.model_copy(deep=True)
    result=GeminiAnalysisResponse(source_summary='Broad',clips=[candidate])
    service=Mock();service.compress_candidate.return_value=[EDLSegment(start_seconds=a,end_seconds=b) for a,b in segments]
    AutoEditDirector.compress(service,object(),result,{'source_duration':200})
    assert candidate.compression_status==expected
    assert candidate.semantic_source_segments==original.source_segments
    if expected=='FAILED':assert candidate.source_segments==original.source_segments
    else:assert candidate.duration_after_compression==45


def test_compression_cannot_bridge_original_gap():
    candidate=clip(142)
    candidate.source_segments=[EDLSegment(start_seconds=0,end_seconds=70),EDLSegment(start_seconds=100,end_seconds=172)]
    service=Mock();service.compress_candidate.return_value=[EDLSegment(start_seconds=40,end_seconds=110)]
    AutoEditDirector.compress(service,object(),GeminiAnalysisResponse(source_summary='Broad',clips=[candidate]),{})
    assert candidate.compression_error_code=='COMPRESSION_OUTSIDE_SEMANTIC_RANGES'
    assert len(candidate.source_segments)==2


def test_worker_essential_failure_truthful(tmp_path,monkeypatch):
    monkeypatch.setattr(job_store,'jobs_dir',tmp_path)
    job={'id':'essential','status':'QUEUED_ANALYSIS'};job_store.set(job['id'],job)
    worker=PipelineWorker();monkeypatch.setattr(worker,'_run_analysis',Mock(side_effect=GenerationFailure({'a':ProviderError(503)})))
    worker._process_analysis(job)
    result=job_store.get(job['id'])
    assert result['error']=='AI_SERVICE_UNAVAILABLE' and result['analysis_retryable']


@pytest.mark.parametrize('outcome',['success','503','invalid'])
def test_cached_broad_compression_worker_reuses_one_active_file(tmp_path,monkeypatch,outcome):
    (tmp_path/'jobs').mkdir()
    monkeypatch.setattr(job_store,'jobs_dir',tmp_path/'jobs')
    monkeypatch.setattr(config,'DATA_CACHE',tmp_path/'cache')
    source=tmp_path/'source.mp4';source.write_bytes(b'fixture content identity')
    settings=AutoEditDirector.settings().model_dump()
    job={'id':'recovery','filepath':str(source),'metadata':{'duration':200,'file_size':32},
         'analysis_settings':settings,'status':'QUEUED_ANALYSIS','preserve_analysis_artifacts':True}
    cache=SemanticCache();key=cache.key(source,settings)
    cache.save(key,GeminiAnalysisResponse(source_summary='Broad succeeded',clips=[clip(142)]),{}, 10)
    job['remote_gemini_file']={'name':'files/same','cache_key':key,'state':'ACTIVE'}
    job_store.set(job['id'],job)
    remote=SimpleNamespace(name='files/same',state='ACTIVE')
    service=Mock();service.client.files.get.return_value=remote;service.analyze_video.return_value=GeminiAnalysisResponse(source_summary='Broad succeeded',clips=[clip(142)])
    if outcome=='503':service.compress_candidate.side_effect=GenerationFailure({'a':ProviderError(503)})
    else:service.compress_candidate.return_value=[EDLSegment(start_seconds=0,end_seconds=50 if outcome=='success' else 70)]
    monkeypatch.setattr('app.services.gemini_service.GeminiService',lambda:service)
    PipelineWorker()._process_analysis(job)
    saved=job_store.get(job['id'])
    service.upload_video.assert_not_called()
    assert service.compress_candidate.call_args.args[0] is remote
    assert saved['status']==('READY_TO_RENDER' if outcome=='success' else 'FAILED')
    if outcome=='503':
        assert saved['error']=='AI_COMPRESSION_SERVICE_UNAVAILABLE'
        assert saved['analysis_retryable']
        service.delete_video.assert_not_called()
        assert cache.load(key)[0].clips[0].source_segments==clip(142).source_segments
    elif outcome=='invalid':assert saved['error']=='ALL_CANDIDATES_REJECTED'
    else:service.delete_video.assert_called_once_with('files/same')


def test_zero_duration_asr_word_does_not_remove_other_captions():
    from app.services.caption_director import CaptionDirector
    from app.services.timeline import with_output_times
    candidate = clip()
    shots = with_output_times([{"start":20.,"end":30.}])
    source = [{"start_seconds":22.,"end_seconds":23.,"text":"Real words"},
              {"start_seconds":23.,"end_seconds":23.,"text":"Um,"},
              {"start_seconds":24.,"end_seconds":26.,"text":"More real words"}]
    captions, plan = CaptionDirector.plan(candidate, source, shots, [])
    assert captions == [dict(source[0],emphasis=[],motion="APPEAR"), dict(source[2],emphasis=[],motion="APPEAR")]
    assert plan["status"] == "READY" and plan["unit_count"] == 2
    assert plan["positions"][0]["end"] == 10
    with pytest.raises(ValueError, match="INVALID_CAPTION_TIME"):
        CaptionDirector.plan(candidate, [{"start_seconds":23.,"end_seconds":22.,"text":"bad"}], shots, [])


def test_render_retry_does_not_run_gemini_analysis(monkeypatch):
    worker = PipelineWorker()
    analysis = Mock(side_effect=AssertionError("Render retry must not invoke analysis"))
    render = Mock()
    monkeypatch.setattr(worker,"_process_analysis",analysis)
    monkeypatch.setattr(worker,"_process_render",render)
    monkeypatch.setattr("app.services.worker.heavy_gate",Mock())
    monkeypatch.setattr("app.services.worker.storage_lifecycle.require_processing_space",Mock())
    monkeypatch.setattr("app.services.transcription_service.TranscriptionService.release_model",Mock())
    job = {"id":"render-retry-no-ai","status":"QUEUED_RENDER"}
    job_store.set(job["id"],job)
    worker._process_job(job)
    render.assert_called_once_with(job)
    analysis.assert_not_called()
