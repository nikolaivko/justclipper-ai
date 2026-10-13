"""Explicit, small live diagnostics. Outputs stay in ignored project logs.

Run with PYTHONPATH=. .venv/bin/python scripts/probe_gemini_models.py.
Never prints credentials; does not download, render, or publish anything.
"""
import json
import argparse
from pathlib import Path
import time
import requests

from google.genai import types
from app.config import config
from app.services.gemini_service import GeminiService


def current_schema():
    from app.services.gemini_schemas import full_analysis_schema
    return full_analysis_schema()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog-only", action="store_true")
    parser.add_argument("--models", nargs="+", default=["gemini-3.5-flash-lite", "gemini-3.8-flash"])
    args = parser.parse_args()
    root = config.PROJECT_ROOT / "logs/gemini-probe"
    root.mkdir(exist_ok=True)
    service = GeminiService()
    # SDK 0.3 lists tunedModels only; base models use the documented REST catalog.
    models, page = [], None
    while True:
        response = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key":config.GEMINI_API_KEY},
            params={"pageToken":page} if page else {}, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"Model catalog HTTP {response.status_code}")
        data = response.json()
        models.extend(data.get("models", []))
        page = data.get("nextPageToken")
        if not page:
            break
    catalog = {"models":models}
    (config.PROJECT_ROOT / "logs/gemini-model-catalog.json").write_text(json.dumps(catalog,indent=2))
    print("CATALOG",len(models),"models",flush=True)
    if args.catalog_only:
        return
    available = {m["name"].removeprefix("models/") for m in catalog["models"] if "generateContent" in m.get("supportedGenerationMethods", [])}
    schema = current_schema()
    (root / "original-schema.json").write_text(json.dumps(schema, indent=2))
    video_path = config.PROJECT_ROOT / "data/input/b418600b_tiny_test.mp4"
    state_path = root / "remote-file.json"
    if state_path.exists():
        video = service.client.files.get(name=json.loads(state_path.read_text())["name"])
    else:
        video = service.upload_video(video_path)
        state_path.write_text(json.dumps({"name": video.name}))
    print("FILE", getattr(video.state, "name", video.state), "local_bytes", video_path.stat().st_size, flush=True)
    video = types.Part(file_data=types.FileData(file_uri=video.uri, mime_type=video.mime_type))
    results = []
    small = {"type":"OBJECT","properties":{"summary":{"type":"STRING"}},"required":["summary"]}
    simple_auto = {"type":"OBJECT","properties":{"source_summary":{"type":"STRING"},"clips":{"type":"ARRAY","items":{"type":"OBJECT","properties":{"title":{"type":"STRING"},"hook":{"type":"STRING"},"payoff":{"type":"STRING"}},"required":["title","hook","payoff"]}}},"required":["source_summary","clips"]}
    stages = [
        ("A_text", "Return OK", None),
        ("B_video", [video,"Briefly describe what happens in this video."], None),
        ("C_system", [video,"Briefly describe what happens in this video."], types.GenerateContentConfig(system_instruction="Describe only visible evidence.")),
        ("D_json", [video,"Describe the video in summary."], types.GenerateContentConfig(response_mime_type="application/json",response_schema=small)),
        ("E_small_editor", [video,"Identify at most one factual moment; empty clips is valid."], types.GenerateContentConfig(response_mime_type="application/json",response_schema=simple_auto)),
        ("F_full_schema", [video,"Identify at most one factual moment; empty clips is valid."], types.GenerateContentConfig(response_mime_type="application/json",response_schema=schema)),
    ]
    for model in args.models:
        assert model in available
        for stage, contents, generation in stages:
            try:
                r = service.client.models.generate_content(model=model, contents=contents, config=generation)
                record = {"model":model,"stage":stage,"status":"SUCCESS","text":r.text}
            except Exception as exc:
                record = {"model":model,"stage":stage,"status":getattr(exc,"code",type(exc).__name__),"error":str(exc).replace(config.GEMINI_API_KEY,"[REDACTED]")[:4000]}
            results.append(record)
            (root / "explicit-file-results.json").write_text(json.dumps(results,indent=2))
            print(json.dumps(record),flush=True)
            if record["status"] != "SUCCESS":
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
