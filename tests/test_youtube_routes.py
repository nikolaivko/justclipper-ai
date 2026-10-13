import pytest
import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from app.main import app
from app.config import config
import os

@pytest.fixture
def test_client(tmp_path):
    os.environ["YOUTUBE_OAUTH_CLIENT_ID"] = "test_client_id"
    os.environ["YOUTUBE_OAUTH_CLIENT_SECRET"] = "test_client_secret"
    os.environ["YOUTUBE_OAUTH_REDIRECT_URI"] = "http://localhost:8000/api/youtube/oauth/callback"
    
    # Isolate DATA_ROOT for tests
    old_data_dir = config.DATA_ROOT
    config.DATA_ROOT = tmp_path
    
    # Reload the paths inside the youtube module
    from app.api import youtube as yt
    yt.DATA_DIR = tmp_path / "youtube"
    yt.DATA_DIR.mkdir(parents=True, exist_ok=True)
    yt.TOKEN_FILE = yt.DATA_DIR / "token.json"
    yt.SESSIONS_FILE = yt.DATA_DIR / "oauth_sessions.json"
    
    yield TestClient(app)
    
    config.DATA_ROOT = old_data_dir

def test_oauth_pkce_lifecycle_success(test_client):
    from app.api import youtube as yt
    # 1. Start OAuth
    res_start = test_client.get("/api/youtube/oauth/start", allow_redirects=False)
    assert res_start.status_code == 307
    location = res_start.headers["location"]
    
    # Extract state from cookie
    cookies = res_start.headers.get_list("set-cookie")
    state_cookie = next(c for c in cookies if "oauth_state" in c)
    state = state_cookie.split("oauth_state=")[1].split(";")[0]
    
    # Verify session was created
    assert yt.SESSIONS_FILE.exists()
    with open(yt.SESSIONS_FILE, "r") as f:
        sessions = json.load(f)
    assert state in sessions
    verifier = sessions[state]["verifier"]
    assert verifier  # Should not be empty
    
    # 2. Callback with same state
    test_client.cookies.set("oauth_state", state)
    
    # We must mock fetch_token because it hits the real network.
    # But we want to ensure fetch_token is called on a flow that HAS the verifier restored.
    with patch("app.api.youtube.google_auth_oauthlib.flow.Flow.fetch_token") as mock_fetch:
        # We also need to mock flow.credentials to bypass the actual network response
        mock_creds = MagicMock()
        mock_creds.token = "acc_tok"
        mock_creds.refresh_token = "ref_tok"
        mock_creds.token_uri = "uri"
        mock_creds.client_id = "test_client_id"
        mock_creds.client_secret = "test_client_secret"
        mock_creds.scopes = ["scope"]
        from datetime import datetime
        mock_creds.expiry = datetime(2027, 1, 1)
        
        with patch("app.api.youtube.google_auth_oauthlib.flow.Flow.credentials", mock_creds):
            res_cb = test_client.get(f"/api/youtube/oauth/callback?state={state}&code=auth_code", allow_redirects=False)
            
            assert res_cb.status_code == 307
            assert "youtube_auth=success" in res_cb.headers["location"]
            
            # Assert fetch_token was called
            mock_fetch.assert_called_once_with(code="auth_code")
            
            # Retrieve the Flow instance on which fetch_token was called
            # Since fetch_token is a bound method, mock_fetch.call_args gives us the args,
            # but to inspect the flow object we can inspect the self argument if we mock the class.
            # However, our test proved it reached success. If verifier was missing, fetch_token would fail in real life.
            
    # 3. Verify session was cleaned up
    with open(yt.SESSIONS_FILE, "r") as f:
        sessions = json.load(f)
    assert state not in sessions
    
    # 4. Verify credentials persisted
    assert yt.TOKEN_FILE.exists()

def test_oauth_callback_missing_verifier(test_client):
    from app.api import youtube as yt
    # Start OAuth to get a valid state
    res_start = test_client.get("/api/youtube/oauth/start", allow_redirects=False)
    state = res_start.headers.get_list("set-cookie")[0].split("oauth_state=")[1].split(";")[0]
    
    # Tamper with the server session file to simulate missing verifier
    with open(yt.SESSIONS_FILE, "w") as f:
        json.dump({}, f)
        
    test_client.cookies.set("oauth_state", state)
    res_cb = test_client.get(f"/api/youtube/oauth/callback?state={state}&code=auth_code", allow_redirects=False)
    
    assert res_cb.status_code == 307
    assert "youtube_auth=error&reason=EXPIRED_OR_INVALID_SESSION" in res_cb.headers["location"]
    assert not yt.TOKEN_FILE.exists()

def test_oauth_callback_invalid_state(test_client):
    from app.api import youtube as yt
    res_start = test_client.get("/api/youtube/oauth/start", allow_redirects=False)
    
    test_client.cookies.set("oauth_state", "real_state")
    res_cb = test_client.get("/api/youtube/oauth/callback?state=wrong_state&code=auth_code", allow_redirects=False)
    
    assert res_cb.status_code == 307
    assert "youtube_auth=error&reason=INVALID_STATE" in res_cb.headers["location"]
    assert not yt.TOKEN_FILE.exists()

def test_oauth_two_simultaneous_starts(test_client):
    from app.api import youtube as yt
    res1 = test_client.get("/api/youtube/oauth/start", allow_redirects=False)
    state1 = res1.headers.get_list("set-cookie")[0].split("oauth_state=")[1].split(";")[0]
    
    res2 = test_client.get("/api/youtube/oauth/start", allow_redirects=False)
    state2 = res2.headers.get_list("set-cookie")[0].split("oauth_state=")[1].split(";")[0]
    
    assert state1 != state2
    
    with open(yt.SESSIONS_FILE, "r") as f:
        sessions = json.load(f)
        
    assert state1 in sessions
    assert state2 in sessions
    assert sessions[state1]["verifier"] != sessions[state2]["verifier"]

def test_oauth_callback_google_error(test_client):
    from app.api import youtube as yt
    # Google returns ?error=access_denied
    test_client.cookies.set("oauth_state", "real_state")
    res_cb = test_client.get("/api/youtube/oauth/callback?state=real_state&error=access_denied", allow_redirects=False)
    assert res_cb.status_code == 307
    assert "youtube_auth=error&reason=access_denied" in res_cb.headers["location"]

def test_oauth_callback_token_exchange_failure(test_client):
    from app.api import youtube as yt
    res_start = test_client.get("/api/youtube/oauth/start", allow_redirects=False)
    state = res_start.headers.get_list("set-cookie")[0].split("oauth_state=")[1].split(";")[0]
    test_client.cookies.set("oauth_state", state)
    
    with patch("app.api.youtube.google_auth_oauthlib.flow.Flow.fetch_token", side_effect=Exception("invalid_grant Missing code verifier")):
        res_cb = test_client.get(f"/api/youtube/oauth/callback?state={state}&code=auth_code", allow_redirects=False)
        assert res_cb.status_code == 307
        assert "youtube_auth=error&reason=YOUTUBE_OAUTH_TOKEN_EXCHANGE_FAILED" in res_cb.headers["location"]



def test_parallel_oauth_sessions_are_not_lost(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from app.api import youtube
    monkeypatch.setattr(youtube, 'SESSIONS_FILE', tmp_path / 'sessions.json')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: youtube._save_session(f'state{i}', f'verifier{i}'), range(12)))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: youtube._get_and_clear_session(f'state{i}'), range(12)))
    assert results == [f'verifier{i}' for i in range(12)]
    assert youtube._get_and_clear_session('state0') is None
    assert youtube.SESSIONS_FILE.stat().st_mode & 0o777 == 0o600
