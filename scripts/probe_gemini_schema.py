"""Explicit bounded live comparison against the archived failing schema.

Uses one already uploaded project fixture. Run only to diagnose compatibility.
"""
import json
import time
from app.config import config
from app.services.gemini_service import GeminiService
from app.services.gemini_requests import video_contents, classify_error
from app.services.gemini_schemas import analysis_schema, full_analysis_schema
from google.genai import types


def main():
    root = config.PROJECT_ROOT / "logs/gemini-probe"
    service = GeminiService()
    video = service.client.files.get(name=json.loads((root/"remote-file.json").read_text())["name"])
    results = []
    for label, schema in [("full",full_analysis_schema()),("broad",analysis_schema("broad")),("deep",analysis_schema("deep"))]:
        try:
            result = service.client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=video_contents([video,"Inspect this video. Return at most one factual candidate with hook, setup, payoff and visual evidence. Empty clips is valid."]),
                config=types.GenerateContentConfig(response_mime_type="application/json",response_schema=schema))
            record = {"stage":label,"status":"SUCCESS","text":result.text}
        except Exception as exc:
            record = {"stage":label,"status":classify_error(exc)[0],"category":classify_error(exc)[1],
                      "error":str(exc).replace(config.GEMINI_API_KEY,"[REDACTED]")[:2000]}
        results.append(record)
        (root/"phase-schema-results.json").write_text(json.dumps(results,indent=2))
        print(json.dumps(record),flush=True)
        if record.get("status") in (429,503):
            break
        time.sleep(1)


if __name__ == "__main__":
    main()
