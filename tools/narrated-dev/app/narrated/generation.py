"""Bounded Gemini retry/fallback for Narrated, reusing one uploaded file."""
import time
import httpx


def generate(client, config, contents, schema, notify):
    models = config.gemini_models(config.GEMINI_MODEL)[:4]
    failures = []
    for model in models:
        for attempt in range(2):
            notify(f'Writing script · {model} · attempt {attempt+1}/2')
            try:
                from google.genai import types
                return client.models.generate_content(model=model, contents=contents,
                    config=types.GenerateContentConfig(response_mime_type='application/json', response_schema=schema))
            except Exception as exc:
                code = getattr(exc, 'code', None)
                try: code = int(code)
                except (ValueError, TypeError): code = None
                transient = code in {429, 500, 502, 503, 504} or isinstance(exc, (TimeoutError, httpx.TimeoutException, httpx.NetworkError))
                if not transient and code != 404:
                    raise
                failures.append(code)
                if code in {404, 429} or attempt:
                    break
                notify(f'Gemini is busy · retrying {model} shortly')
                time.sleep(5)
    raise RuntimeError('Gemini is temporarily unavailable or rate limited. Your source is saved. Use Retry script without uploading again.')
