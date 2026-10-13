import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google import genai
from google.genai import types

from app.config import Config
from app.services.gemini_requests import GenerationSession, GenerationFailure, classify_error, video_contents, generate_with_windows
from app.services.gemini_schemas import analysis_schema, full_analysis_schema
from app.services.gemini_service import GeminiService


class APIError(Exception):
    def __init__(self, code, message="provider error"):
        self.code = code
        super().__init__(message)


@pytest.mark.parametrize("code,message,category", [
    (400,"invalid argument","REQUEST_INCOMPATIBLE"),
    (429,"resource exhausted","UNKNOWN_RATE_LIMIT"),
    (429,"limit: 0","ZERO_QUOTA"),
    (503,"high demand, not quota","SERVICE_UNAVAILABLE"),
])
def test_error_classification_uses_status(code, message, category):
    assert classify_error(APIError(code,message)) == (code, category)


def test_verified_small_chain_filters_unknown_ids(monkeypatch):
    monkeypatch.setattr(Config,"GEMINI_FALLBACK_MODELS",("not-a-verified-model","gemini-3.8-flash"))
    models = Config.gemini_models("gemini-3.5-flash-lite")
    assert models[:2] == ["gemini-3.5-flash-lite","gemini-3.8-flash"]
    assert "not-a-verified-model" not in models
    assert "gemini-3.6-flash" in Config.VERIFIED_GEMINI_MODELS


def test_sdk_wire_request_contains_real_file_reference():
    client = genai.Client(api_key="unit-test")
    request = Mock(side_effect=RuntimeError("captured"))
    client.models.api_client.request = request
    file = types.File(name="files/test",uri="https://generativelanguage.googleapis.com/v1beta/files/test",mime_type="video/mp4")
    with pytest.raises(RuntimeError, match="captured"):
        client.models.generate_content(model="gemini-3.5-flash-lite", contents=video_contents([file,"Describe"]))
    body = request.call_args.args[2]
    parts = [p for c in body["contents"] for p in c["parts"]]
    assert {"fileData":{"file_uri":file.uri,"mime_type":"video/mp4"}} in parts
    assert {} not in parts


@pytest.mark.parametrize("code,expected_calls", [(503,3),(429,1)])
def test_dead_model_not_repeated_across_job_phases(monkeypatch, code, expected_calls):
    sleep = Mock()
    monkeypatch.setattr("app.services.gemini_requests.time.sleep",sleep)
    def generate(**kw):
        if kw["model"] == "a": raise APIError(code,"limit: 0" if code==429 else "high demand")
        return SimpleNamespace(text="{}")
    generate = Mock(side_effect=generate)
    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate),files=Mock())
    session = GenerationSession()
    file = types.File(uri="https://example.test/video",mime_type="video/mp4")
    for phase in ("source_map","broad","deep","critic"):
        session.generate(client,["a","b"],[file,phase],phase=phase)
    assert sum(c.kwargs["model"]=="a" for c in generate.call_args_list)==expected_calls
    assert all(c.kwargs["contents"][0].file_data.file_uri == file.uri for c in generate.call_args_list)
    client.files.upload.assert_not_called()
    assert sleep.call_count == (2 if code==503 else 0)
    # A new job gets a fresh budget.
    assert not GenerationSession().unavailable


def test_400_blocks_config_not_all_requests():
    generate = Mock(side_effect=APIError(400))
    session = GenerationSession()
    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    schema = types.GenerateContentConfig(response_mime_type="application/json",response_schema={"type":"STRING"})
    for prompt in ("first", "different candidate"):
        with pytest.raises(GenerationFailure): session.generate(client,["a"],prompt,schema)
    assert generate.call_count==1
    with pytest.raises(GenerationFailure): session.generate(client,["a"],"plain text")
    assert generate.call_count==2


def test_mixed_failures_report_all_categories(monkeypatch):
    monkeypatch.setattr("app.services.gemini_requests.time.sleep",Mock())
    generate=Mock(side_effect=[APIError(400),APIError(503),APIError(503),APIError(503)])
    with pytest.raises(GenerationFailure) as error:
        GenerationSession().generate(SimpleNamespace(models=SimpleNamespace(generate_content=generate)),["a","b"],"text")
    assert "REQUEST_INCOMPATIBLE" in str(error.value) and "SERVICE_UNAVAILABLE" in str(error.value)
    assert "quota" not in str(error.value)


def test_source_map_diagnostic_has_model_status_category_without_secrets(monkeypatch,caplog):
    service=GeminiService()
    service.client=SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=APIError(503,"secret-value"))))
    monkeypatch.setattr(service,"_model_chain",lambda model:[model])
    monkeypatch.setattr("app.services.gemini_requests.time.sleep",Mock())
    with pytest.raises(GenerationFailure): service.map_source(object(),{"source_duration":2},["gemini-3.8-flash"])
    assert "phase=source_map" in caplog.text and "http=503" in caplog.text
    assert "category=SERVICE_UNAVAILABLE" in caplog.text and "model=gemini-3.8-flash" in caplog.text
    assert "secret-value" not in caplog.text


def test_actual_phase_schemas_preserve_semantics_and_serialize():
    full=full_analysis_schema()
    for phase in ("broad","deep"):
        schema=analysis_schema(phase)
        encoded=json.dumps(schema)
        types.GenerateContentConfig(response_schema=schema)
        assert len(encoded)<len(json.dumps(full))
        properties=schema["properties"]["clips"]["items"]["properties"]
        required=schema["properties"]["clips"]["items"]["required"]
        assert {"payoff_present","story_arc","reaction_moments","visual_events"} <= set(required)
        assert {"story_arc","source_segments","payoff_timestamp","reaction_moments","visual_events"} <= properties.keys()
        assert all(construct not in encoded for construct in ('"$ref"','"$defs"','"anyOf"','"oneOf"','"default"','"additionalProperties"'))
    assert {"micro_beats","hook_variants","effect_events","protected_regions"} <= analysis_schema("deep")["properties"]["clips"]["items"]["properties"].keys()


def test_reset_circuit_for_next_analysis(monkeypatch):
    service=GeminiService()
    service._generation_session=GenerationSession()
    service._generation_session.unavailable[service.model_name]=APIError(503)
    generate=Mock(return_value=SimpleNamespace(text='{"source_summary":"empty","clips":[]}'))
    service.client=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    result=service.analyze_video(object(),{"exact_clip_count":True})
    assert result.source_summary=="empty" and generate.call_count==2


def test_windowed_transport_and_new_response_fields():
    request=Mock(return_value={"candidates":[{"content":{"role":"model","parts":[{"text":"{}"}]}}],
                               "usageMetadata":{"thoughtsTokenCount":5,"totalTokenCount":10}})
    client=SimpleNamespace(models=SimpleNamespace(api_client=SimpleNamespace(request=request)))
    part=types.Part(file_data=types.FileData(file_uri="https://example.test/video",mime_type="video/mp4"),
                    video_metadata=types.VideoMetadata(start_offset="2s",end_offset="8s"))
    response=generate_with_windows(client,"gemini-3.5-flash-lite",[part,"Inspect window"],
                                   types.GenerateContentConfig(response_mime_type="application/json"))
    assert response.text=="{}"
    method,path,body,_=request.call_args.args
    assert method=="post" and path=="models/gemini-3.5-flash-lite:generateContent"
    assert body["contents"][0]["parts"][0]["videoMetadata"]=={"startOffset":"2s","endOffset":"8s"}
    assert body["generationConfig"]["responseMimeType"]=="application/json"


def test_real_request_context_accounting(monkeypatch):
    response = SimpleNamespace(text="{}")
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(return_value=response)))
    monkeypatch.setattr("app.services.gemini_requests.generate_with_windows", Mock(return_value=response))
    session = GenerationSession()
    session.source_duration = 1812.8
    file = types.File(uri="https://example.test/video", mime_type="video/mp4")
    session.generate(client, ["a"], [file, "full"], phase="broad")
    part = types.Part(file_data=types.FileData(file_uri=file.uri, mime_type=file.mime_type),
        video_metadata=types.VideoMetadata(start_offset="620s", end_offset="940s"))
    session.generate(client, ["a"], [part, "window"], phase="deep")
    session.generate(client, ["a"], ["text only"], phase="critic")
    assert [(e["video_window_start"],e["video_window_end"],e["video_context_seconds"])
            for e in session.diagnostics] == [(0,1812.8,1812.8),(620,940,320),(None,None,0)]


def test_generation_transport_has_timeout_and_preserves_files(monkeypatch):
    from app.services.gemini_requests import bound_generation_transport
    import requests
    client = genai.Client(api_key='unit-test')
    transport = client.models.api_client
    original = Mock(return_value='files-unchanged')
    transport._request_unauthorized = original
    send = Mock(side_effect=requests.exceptions.ReadTimeout('no response'))
    fake_session = Mock()
    fake_session.__enter__ = Mock(return_value=fake_session)
    fake_session.__exit__ = Mock(return_value=False)
    fake_session.request = send
    monkeypatch.setattr(requests, 'Session', Mock(return_value=fake_session))
    bound_generation_transport(client)
    with pytest.raises(requests.exceptions.ReadTimeout):
        client.models.generate_content(model='test', contents='test')
    assert send.call_args.kwargs['timeout'] == (20, 180)
    assert transport._request_unauthorized(SimpleNamespace(url='https://example.test/files')) == 'files-unchanged'


def test_request_timeout_moves_to_next_model_without_retry(monkeypatch):
    import requests
    call = Mock(side_effect=[requests.exceptions.ReadTimeout(), SimpleNamespace(text='{}')])
    client = SimpleNamespace(models=SimpleNamespace(generate_content=call))
    session = GenerationSession()
    session.generate(client, ['first', 'second'], ['test'])
    assert [c.kwargs['model'] for c in call.call_args_list] == ['first', 'second']
