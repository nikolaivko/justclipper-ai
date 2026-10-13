from app.config import config
from app.services.json_storage import atomic_write_json, record_lock
from fastapi import APIRouter, Request, HTTPException
from typing import Optional
from fastapi.responses import RedirectResponse
import os
import time
import json
import logging
from pathlib import Path
import google_auth_oauthlib.flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

router = APIRouter()

# Path for storing YouTube tokens (using /data which is persistent on Railway)
DATA_DIR = config.DATA_ROOT / "youtube"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_FILE = DATA_DIR / "token.json"
SESSIONS_FILE = DATA_DIR / "oauth_sessions.json"

def _read_sessions():
    try:
        value = json.loads(SESSIONS_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_session(state: str, verifier: str):
    with record_lock(SESSIONS_FILE):
        now = time.time()
        sessions = {k: v for k, v in _read_sessions().items()
                    if now - v.get("time", 0) < 900}
        sessions[state] = {"verifier": verifier, "time": now}
        atomic_write_json(SESSIONS_FILE, sessions, durable=True)


def _get_and_clear_session(state: str) -> str:
    with record_lock(SESSIONS_FILE):
        sessions = _read_sessions()
        session = sessions.pop(state, None)
        atomic_write_json(SESSIONS_FILE, sessions, durable=True)
    if session and time.time() - session.get("time", 0) < 900:
        return session.get("verifier")
    return None


SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]

def _get_client_config():
    client_id = os.environ.get("YOUTUBE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="YouTube OAuth credentials not configured.")
    
    return {
        "web": {
            "client_id": client_id,
            "project_id": "justclipper",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": client_secret,
        }
    }

def _get_redirect_uri(request: Request = None):
    # If explicitly configured in env, use it
    if "YOUTUBE_OAUTH_REDIRECT_URI" in os.environ:
        return os.environ["YOUTUBE_OAUTH_REDIRECT_URI"]
        
    # If running locally, dynamically build it based on the request
    if request:
        return str(request.base_url).rstrip("/") + "/api/youtube/oauth/callback"
        
    return "http://localhost:8000/api/youtube/oauth/callback"

@router.get("/oauth/start")
async def oauth_start(request: Request):
    try:
        client_config = _get_client_config()
        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            client_config,
            scopes=SCOPES
        )
        flow.redirect_uri = _get_redirect_uri(request)
        
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent"  # Ensure we always get a refresh token
        )
        
        _save_session(state, getattr(flow, "code_verifier", ""))
        
        # Save state in a secure HTTPOnly cookie (or session)
        response = RedirectResponse(authorization_url)
        response.set_cookie(key="oauth_state", value=state, httponly=True, secure=request.url.scheme == "https", samesite="lax", max_age=600)
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/oauth/callback")
async def oauth_callback(request: Request, state: str, code: Optional[str] = None, error: Optional[str] = None):
    if error:
        return RedirectResponse(url="/?youtube_auth=error&reason=" + error)

    saved_state = request.cookies.get("oauth_state")
    if not saved_state or saved_state != state:
        return RedirectResponse(url="/?youtube_auth=error&reason=INVALID_STATE")
        
    # Get server-side PKCE verifier
    verifier = _get_and_clear_session(state)
    if not verifier:
        return RedirectResponse(url="/?youtube_auth=error&reason=EXPIRED_OR_INVALID_SESSION")
        
    try:
        client_config = _get_client_config()
        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            state=state
        )
        flow.redirect_uri = _get_redirect_uri(request)
        
        # Restore PKCE verifier BEFORE fetching token
        flow.code_verifier = verifier
        
        # Fetch the tokens
        flow.fetch_token(code=code)
        credentials = flow.credentials
        
        if not credentials.refresh_token:
            # We didn't get a refresh token! This means we can't do offline uploads.
            return RedirectResponse(url="/?youtube_auth=error&reason=REAUTH_REQUIRED_NO_REFRESH_TOKEN")
        
        # Save credentials securely to disk
        creds_data = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": credentials.scopes
        }
        
        if credentials.expiry:
            creds_data["expiry"] = credentials.expiry.isoformat().removesuffix("+00:00") + "Z"
        with record_lock(TOKEN_FILE):
            atomic_write_json(TOKEN_FILE, creds_data, durable=True)

        # Redirect back to the frontend settings page or close window
        response = RedirectResponse(url="/?youtube_auth=success")
        response.delete_cookie("oauth_state")
        return response
    except Exception as e:
        import logging
        logging.exception("OAuth Callback Failed")
        response = RedirectResponse(url="/?youtube_auth=error&reason=YOUTUBE_OAUTH_TOKEN_EXCHANGE_FAILED")
        response.delete_cookie("oauth_state")
        return response

@router.get("/status")
async def get_status():
    if not TOKEN_FILE.exists():
        return {"status": "NOT_CONNECTED"}
        
    try:
        with open(TOKEN_FILE, "r") as f:
            creds_data = json.load(f)
            
        creds = Credentials.from_authorized_user_info(creds_data)
        
        # Check if we need to reauth
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request as GoogleAuthRequest
                try:
                    creds.refresh(GoogleAuthRequest())
                    # Update saved credentials
                    creds_data["token"] = creds.token
                    if creds.expiry:
                        creds_data["expiry"] = creds.expiry.isoformat().removesuffix("+00:00") + "Z"
                    with record_lock(TOKEN_FILE):
                        # A refresh must not restore a disconnected or replaced account.
                        current = json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else {}
                        if current.get("refresh_token") == creds_data.get("refresh_token"):
                            atomic_write_json(TOKEN_FILE, creds_data, durable=True)
                except Exception:
                    return {"status": "REAUTH_REQUIRED"}
            else:
                return {"status": "REAUTH_REQUIRED"}
                
        # Now fetch the channel details
        youtube = build("youtube", "v3", credentials=creds)
        request = youtube.channels().list(
            part="snippet",
            mine=True
        )
        response = request.execute()
        
        if "items" in response and len(response["items"]) > 0:
            channel = response["items"][0]
            return {
                "status": "CONNECTED",
                "channel_id": channel["id"],
                "channel_title": channel["snippet"]["title"]
            }
        else:
            return {"status": "CHANNEL_NOT_FOUND"}
            
    except Exception as e:
        import logging
        logging.exception("Failed to get YouTube status")
        return {"status": "REAUTH_REQUIRED"}

@router.post("/disconnect")
async def disconnect():
    with record_lock(TOKEN_FILE):
        TOKEN_FILE.unlink(missing_ok=True)
    return {"status": "NOT_CONNECTED"}


@router.post("/publish/{job_id}/{rank}")
def publish_render(job_id: str, rank: int):
    from app.services.render_resolution import RenderResolutionError
    from app.services.youtube_publish_service import (
        PUBLISHING_LIMIT_CODES, publishing_lock, publish_one_locked)
    try:
        with publishing_lock(job_id, blocking=False):
            result = publish_one_locked(job_id, rank)
    except BlockingIOError:
        raise HTTPException(status_code=409, detail="ALREADY_PUBLISHING")
    except RenderResolutionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code)
    if result["status"] in ("UPLOADED", "ALREADY_UPLOADED"):
        return {"status": "UPLOADED", "video_id": result["video_id"],
                "message": "Upload successful" if result["status"] == "UPLOADED"
                           else "Video already uploaded"}
    code = result.get("error_code", "UPLOAD_FAILED")
    http_status = (403 if code in ("RIGHTS_BLOCKED", "RIGHTS_UNVERIFIED")
                   else 404 if code in ("JOB_NOT_FOUND", "RENDER_NOT_FOUND", "OUTPUT_FILE_MISSING")
                   else 400 if code in ("RENDER_NOT_COMPLETED", "INVALID_OUTPUT")
                   else 401 if code == "REAUTH_REQUIRED"
                   else 429 if code in PUBLISHING_LIMIT_CODES
                   else 409 if code in ("UPLOAD_IN_PROGRESS", "JOB_NOT_COMPLETED",
                                        "ATTRIBUTION_MISSING", "VIDEO_UNAVAILABLE",
                                        "TITLE_REVIEW_REQUIRED") else 500)
    raise HTTPException(status_code=http_status, detail=code)


@router.post("/publish-all/{job_id}")
def publish_all(job_id: str):
    from app.services.render_resolution import RenderResolutionError
    from app.services.youtube_publish_service import publishing_lock, publish_all_locked
    try:
        with publishing_lock(job_id, blocking=False):
            return publish_all_locked(job_id)
    except BlockingIOError:
        raise HTTPException(status_code=409, detail="ALREADY_PUBLISHING")
    except RenderResolutionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code)
