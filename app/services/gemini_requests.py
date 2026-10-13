"""Per-analysis request health; no uploads, media processing, or persistent state."""
import hashlib
import json
import logging
import re
import time
import random
import httpx
import requests
import types as python_types
from google.genai import types

logger = logging.getLogger(__name__)


def bound_generation_transport(client):
    """Bound SDK 0.3 generation only; leave its Files upload transport intact."""
    transport = client.models.api_client
    original = transport._request_unauthorized

    def bounded(self, request, stream=False):
        if ':generateContent' not in request.url:
            return original(request, stream)
        from google.genai._api_client import HttpResponse, RequestJsonEncoder
        from google.genai import errors
        data = json.dumps(request.data, cls=RequestJsonEncoder) if request.data else None
        with requests.Session() as session:
            with session.request(request.method, request.url, headers=request.headers,
                                 data=data, timeout=(20, 180), stream=False) as response:
                errors.APIError.raise_for_response(response)
                return HttpResponse(response.headers, [response.text])
    transport._request_unauthorized = python_types.MethodType(bounded, transport)


def _safe_label(value):
    """Provider identifiers only; never retain free-form messages or subjects."""
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and len(value) <= 200 and re.fullmatch(r"[A-Za-z0-9_./: -]+", value):
        if not any(word in value.lower() for word in ("authorization", "bearer", "apikey", "api_key", "token=")):
            return value
    return None


def classify_error(exc):
    if isinstance(exc, (ConnectionError, TimeoutError, httpx.TimeoutException, httpx.NetworkError,
                        requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return None, "SERVICE_UNAVAILABLE"
    code = getattr(exc, "code", None)
    try:
        code = int(code)
    except (ValueError, TypeError):
        match = re.search(r"\b(400|401|403|404|429|500|502|503|504)\b", str(exc))
        code = int(match.group()) if match else None
    payload = getattr(exc, "details", None)
    if not isinstance(payload, dict):
        try:
            payload = json.loads(str(exc)[str(exc).index("{"):])
        except (ValueError, TypeError):
            payload = {}
    error = payload.get("error", payload)
    details = error.get("details", []) if isinstance(error, dict) else []
    violations = []
    retry = None
    for detail in details:
        if not isinstance(detail, dict):
            continue
        kind = detail.get("@type", "").rsplit("/", 1)[-1]
        if kind == "google.rpc.QuotaFailure":
            for violation in detail.get("violations", []):
                dimensions = violation.get("quotaDimensions", {})
                violations.append({
                    "quota_metric": _safe_label(violation.get("quotaMetric")),
                    "quota_id": _safe_label(violation.get("quotaId")),
                    "quota_value": _safe_label(violation.get("quotaValue")),
                    "quota_dimensions": {k: _safe_label(v) for k, v in dimensions.items()
                                         if k in {"model", "location", "region", "service"}},
                })
        elif kind == "google.rpc.ErrorInfo" and not violations:
            metadata = detail.get("metadata", {})
            violations.append({"quota_metric": _safe_label(metadata.get("quota_metric")),
                               "quota_id": _safe_label(metadata.get("quota_id")),
                               "quota_value": _safe_label(metadata.get("quota_limit_value")),
                               "quota_dimensions": {}})
        elif kind == "google.rpc.RetryInfo":
            delay = detail.get("retryDelay", "")
            if isinstance(delay, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?s", delay):
                retry = float(delay[:-1])
    quota = next((v for v in violations if v.get("quota_metric") or v.get("quota_id")), {})
    exc.quota_metric = quota.get("quota_metric")
    exc.quota_id = quota.get("quota_id")
    exc.quota_value = quota.get("quota_value")
    exc.quota_dimensions = quota.get("quota_dimensions", {})
    exc.quota_violations = violations
    exc.google_status = _safe_label(error.get("status") or getattr(exc, "status", None))
    exc.retry_delay = retry
    category = {400: "REQUEST_INCOMPATIBLE", 401: "AUTH_REQUIRED", 403: "PERMISSION_DENIED",
                404: "MODEL_UNAVAILABLE", 429: "UNKNOWN_RATE_LIMIT", 500: "SERVICE_UNAVAILABLE",
                502: "SERVICE_UNAVAILABLE", 503: "SERVICE_UNAVAILABLE", 504: "SERVICE_UNAVAILABLE"}.get(code, "GENERATION_FAILED")
    if code == 429:
        identifiers = " ".join(str(v.get(k) or "") for v in violations
                               for k in ("quota_metric", "quota_id")).lower()
        if "perday" in identifiers or "per_day" in identifiers or "daily" in identifiers or getattr(exc, "error_code", "") == "AI_QUOTA_EXHAUSTED":
            category = "QUOTA_EXHAUSTED"
        elif quota.get("quota_value") in (0, "0") or re.search(r"limit\s*[:=]\s*0\b", str(exc).lower()):
            category = "ZERO_QUOTA"
        elif "token" in identifiers and ("minute" in identifiers or "tpm" in identifiers):
            category = "TOKEN_RATE_LIMIT"
        elif "minute" in identifiers or "second" in identifiers or retry is not None and retry <= 60:
            category = "SHORT_RATE_LIMIT"
    return code, category


def video_contents(contents):
    """SDK 0.3 silently serializes File as an empty Part; supply file_data explicitly."""
    if isinstance(contents, types.File):
        if not contents.uri:
            raise ValueError("Gemini File has no URI")
        return types.Part(file_data=types.FileData(file_uri=contents.uri,
                                                  mime_type=contents.mime_type or "video/mp4"))
    if isinstance(contents, list):
        return [video_contents(item) for item in contents]
    return contents


def generate_with_windows(client, model, contents, config):
    """SDK 0.3 rejects videoMetadata despite REST support. Reuse its authenticated
    transport only for windowed generateContent; Files methods stay untouched.
    """
    parts = [types.Part(text=p) if isinstance(p, str) else p for p in contents]
    body = {"contents": [{"role": "user", "parts": [
        p.model_dump(mode="json", by_alias=True, exclude_none=True) for p in parts]}]}
    generation = config.model_dump(mode="json", by_alias=True, exclude_none=True) if config else {}
    supported = {"responseMimeType", "responseSchema", "temperature", "topP", "topK", "maxOutputTokens"}
    if set(generation) - supported:
        raise ValueError("Unsupported windowed generation configuration")
    body["generationConfig"] = generation
    raw = client.models.api_client.request("post", f"models/{model}:generateContent", body, None)
    # Use the pinned SDK's normal response converter as well: new server fields
    # such as thoughtsTokenCount are not part of its strict 0.3 response models.
    from google.genai.models import _GenerateContentResponse_from_mldev
    return types.GenerateContentResponse._from_response(
        _GenerateContentResponse_from_mldev(client.models.api_client, raw), {})


class GenerationFailure(RuntimeError):
    def __init__(self, failures, diagnostics=None):
        self.failures = failures
        categories = sorted({classify_error(e)[1] for e in failures.values()})
        self.categories = categories
        self.transient = bool(set(categories) & {"SERVICE_UNAVAILABLE", "SHORT_RATE_LIMIT", "UNKNOWN_RATE_LIMIT", "TOKEN_RATE_LIMIT"})
        self.error_code = "AI_QUOTA_EXHAUSTED" if "QUOTA_EXHAUSTED" in categories else ("AI_SERVICE_UNAVAILABLE" if self.transient else "AI_GENERATION_FAILED")
        self.diagnostics = list(diagnostics or [])
        if categories == ["SERVICE_UNAVAILABLE"]:
            message = "AI analysis is temporarily unavailable due to high demand."
        elif set(categories) <= {"SHORT_RATE_LIMIT", "UNKNOWN_RATE_LIMIT", "TOKEN_RATE_LIMIT", "ZERO_QUOTA", "QUOTA_EXHAUSTED"}:
            message = "AI analysis has no available API quota or is rate limited."
        elif categories == ["REQUEST_INCOMPATIBLE"]:
            message = "AI analysis request is incompatible with the configured models."
        else:
            message = "AI analysis failed: " + ", ".join(categories)
        super().__init__(message)


class TransientGenerationFailure(GenerationFailure):
    """Configured model chain exhausted by a retryable provider failure."""


def transient_failure(exc):
    return (isinstance(exc, GenerationFailure) and
            (exc.transient or bool(set(exc.categories) & {"QUOTA_EXHAUSTED", "ZERO_QUOTA"})) or
            classify_error(exc)[1] in {"SERVICE_UNAVAILABLE", "SHORT_RATE_LIMIT", "UNKNOWN_RATE_LIMIT", "TOKEN_RATE_LIMIT", "QUOTA_EXHAUSTED"})


def failure_category(exc):
    if isinstance(exc, GenerationFailure):
        return exc.categories[0] if len(exc.categories) == 1 else "GENERATION_FAILED"
    return classify_error(exc)[1]


import time
_GLOBAL_QUOTA_UNAVAILABLE_UNTIL = {}

class GenerationSession:
    """Owned by one job, shared by source-map, broad, deep and critic passes."""
    def __init__(self, on_event=None):
        self.unavailable = {}
        self.incompatible = {}
        self.last_model = None
        self.diagnostics = []
        self.on_event = on_event
        self.request_number = 0
        self.source_duration = None
        self.request_context = {}

    def record(self, event):
        event = dict(self.request_context, **event, timestamp=time.time(), request_id=self.request_number)
        self.diagnostics.append(event)
        if self.on_event:
            self.on_event(event)

    def generate(self, client, models, contents, config=None, phase="analysis"):
        self.request_number += 1
        contents = video_contents(contents)
        wire_config = config.model_dump(mode="json", exclude_none=True) if config else {}
        # Schema/config + input modality, not changing prompt text or timestamps.
        video = isinstance(contents, list) and any(isinstance(p, types.Part) and p.file_data for p in contents)
        windowed = isinstance(contents, list) and any(
            isinstance(p, types.Part) and p.video_metadata for p in contents)
        signature = hashlib.sha256(json.dumps([wire_config, bool(video), bool(windowed)], sort_keys=True).encode()).hexdigest()
        start, end = (0.0, self.source_duration) if video else (None, None)
        if windowed:
            part = next(p for p in contents if isinstance(p, types.Part) and p.video_metadata)
            start = float(part.video_metadata.start_offset.rstrip("s"))
            end = float(part.video_metadata.end_offset.rstrip("s"))
        self.request_context = dict(video_window_start=start, video_window_end=end,
                                    video_context_seconds=end - start if start is not None and end is not None else (None if video else 0))
        failures = {}
        for model in models:
            if _GLOBAL_QUOTA_UNAVAILABLE_UNTIL.get(model, 0) > __import__("time").time():
                skipped = RuntimeError(f"Model {model} is globally exhausted.")
                skipped.code = 429
                skipped.error_code = "AI_QUOTA_EXHAUSTED"
                failures[model] = skipped
                self.record(dict(phase=phase, model=model, category="SKIPPED_UNAVAILABLE",
                                 attempt=0, http_status=429, cause_category="QUOTA_EXHAUSTED", structured_output=None))
                continue
            skipped = self.unavailable.get(model) or self.incompatible.get((model, signature))

            if skipped:
                failures[model] = skipped
                code, category = classify_error(skipped)
                self.record(dict(phase=phase, model=model, category="SKIPPED_UNAVAILABLE",
                                 attempt=0, http_status=code, cause_category=category, structured_output=None))
                continue
            for attempt in range(3):
                try:
                    if self.on_event:
                        self.on_event(dict(self.request_context, phase=phase, model=model,
                            category="REQUEST_STARTED", attempt=attempt + 1, http_status=None,
                            structured_output=None, timestamp=time.time(), request_id=self.request_number))
                    response = (generate_with_windows(client, model, contents, config) if windowed else
                                client.models.generate_content(model=model, contents=contents, config=config))
                    self.last_model = model
                    self.record(dict(phase=phase, model=model, http_status=200,
                                     category="SUCCESS", attempt=attempt + 1,
                                     structured_output="PENDING_VALIDATION"))
                    return response
                except Exception as exc:
                    code, category = classify_error(exc)
                    quota_metric = getattr(exc, "quota_metric", None)
                    quota_id = getattr(exc, "quota_id", None)
                    quota_value = getattr(exc, "quota_value", None)
                    retry_delay = getattr(exc, "retry_delay", None)
                    failures[model] = exc
                    diagnostic = dict(phase=phase, model=model, http_status=code,
                                      category=category, attempt=attempt + 1)
                    for field in ("quota_metric", "quota_id", "quota_value", "quota_dimensions", "quota_violations", "google_status", "retry_delay"):
                        diagnostic[field] = getattr(exc, field, None)
                    diagnostic["structured_output"] = False
                    self.record(diagnostic)
                    if isinstance(exc, (requests.exceptions.Timeout, httpx.TimeoutException, TimeoutError)):
                        self.unavailable[model] = exc
                        break
                    if category in ("QUOTA_EXHAUSTED", "TOKEN_RATE_LIMIT"):
                        logger.warning("Gemini request failed: phase=%s model=%s http=%s category=%s attempt=%s quotaMetric=%s quotaId=%s retryDelay=%s",
                                       phase, model, code, category, attempt + 1, quota_metric, quota_id, retry_delay)
                    else:
                        logger.warning("Gemini request failed: phase=%s model=%s http=%s category=%s attempt=%s",
                                       phase, model, code, category, attempt + 1)
                    if category == "QUOTA_EXHAUSTED":
                        # Mark this specific model unavailable immediately, do NOT retry 3 times!
                        self.unavailable[model] = exc
                        if retry_delay:
                            _GLOBAL_QUOTA_UNAVAILABLE_UNTIL[model] = time.time() + retry_delay
                        break
                    if category == "REQUEST_INCOMPATIBLE":
                        self.incompatible[model, signature] = exc
                        break
                    if category == "SERVICE_UNAVAILABLE" or (category in {"SHORT_RATE_LIMIT", "UNKNOWN_RATE_LIMIT"} and (retry_delay or 0) <= 20):
                        if attempt < 2:
                            time.sleep(min(20, max(retry_delay or 0, 5 * 2 ** attempt + random.uniform(0, 1))))
                            continue
                        self.unavailable[model] = exc
                    elif code in (401, 403, 404, 429):
                        # Rate-limit responses are not hammered; next job may retry.
                        self.unavailable[model] = exc
                    elif code is None:
                        # A local programming/validation error is not provider capacity.
                        raise
                    break
        failure_type = TransientGenerationFailure if any(transient_failure(e) for e in failures.values()) else GenerationFailure
        failure = failure_type(failures, [d for d in self.diagnostics if d["request_id"] == self.request_number])
        raise failure from (list(failures.values())[-1] if failures else None)
