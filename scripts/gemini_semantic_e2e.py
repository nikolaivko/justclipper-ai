"""Explicit semantic-only live check using existing project-local media. No publish."""
import argparse
import json
from pathlib import Path
from app.config import config
from app.services.gemini_service import GeminiService
from app.services.auto_edit_director import AutoEditDirector

parser = argparse.ArgumentParser()
parser.add_argument("--existing-job", action="store_true")
parser.add_argument("--deep-only", action="store_true")
args = parser.parse_args()
root = config.PROJECT_ROOT / "logs/gemini-probe"
root.mkdir(exist_ok=True)
if args.existing_job:
    job = json.loads((config.DATA_JOBS / "job_youtube_4bZzhSl-wXU.json").read_text())
    source = Path(job["filepath"]).resolve()
    duration = job["metadata"]["duration"]
    label = "existing-job"
else:
    source = config.PROJECT_ROOT / "data/input/test_short.mp4"
    duration = 11.04
    label = "tiny-semantic"
assert source.is_relative_to(config.PROJECT_ROOT.resolve()) and source.is_file()
service = GeminiService()
state = root / (label + "-file.json")
if state.exists():
    video = service.client.files.get(name=json.loads(state.read_text())["name"])
else:
    video = service.upload_video(source)
    state.write_text(json.dumps({"name":video.name}))
print("FILE", getattr(video.state,"name",video.state), "bytes",source.stat().st_size,flush=True)
settings = AutoEditDirector.settings().model_dump()
settings["source_duration"] = duration
try:
    if args.deep_only:
        from app.models.schemas import GeminiAnalysisResponse
        from app.services.gemini_schemas import analysis_schema
        from app.services.gemini_requests import GenerationSession
        result = GeminiAnalysisResponse.model_validate_json((root/(label+"-result.json")).read_text())
        original = service.client.models.api_client.request
        def diagnostic_request(*a,**kw):
            raw = original(*a,**kw)
            (root/(label+"-window-response.json")).write_text(json.dumps(raw,indent=2))
            return raw
        service.client.models.api_client.request = diagnostic_request
        service._generation_session = GenerationSession()
        service._deepen_and_critique(video,result,analysis_schema("deep"),service.model_name,duration,
                                    limit=config.AUTO_EDIT_DEEP_CANDIDATES if args.existing_job else 1)
    else:
        result = AutoEditDirector.understand(service,video,settings)
    (root/(label+"-result.json")).write_text(result.model_dump_json(indent=2))
    print(json.dumps({"summary":result.source_summary,"models":result.analysis_models,"source_map":result.source_map,
                      "candidates":[{"title":c.title,"segments":[s.model_dump() for s in c.source_segments],"deep":c.deep_analysis_status} for c in result.clips]},ensure_ascii=False),flush=True)
except Exception as exc:
    print("FAILED",type(exc).__name__,str(exc).replace(config.GEMINI_API_KEY,"[REDACTED]")[:2000],flush=True)
finally:
    (root/(label+"-health.json")).write_text(json.dumps(service._generation_session.diagnostics,indent=2))
