"""Publish a narrated output with Clipper's existing OAuth grant, in isolation."""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import httplib2
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

ROOT = Path(__file__).resolve().parents[1]



def find_existing(youtube, options, folder, since):
    """Reconcile an ambiguous completion; never start a second upload."""
    channels = youtube.channels().list(part='contentDetails', mine=True).execute()
    items = channels.get('items', [])
    if not items:
        return None
    playlist = items[0]['contentDetails']['relatedPlaylists']['uploads']
    rows = youtube.playlistItems().list(part='snippet', playlistId=playlist, maxResults=50).execute()
    ids = [row['snippet']['resourceId']['videoId'] for row in rows.get('items', [])
           if row['snippet'].get('title') == options['title']]
    if not ids:
        return None
    expected = float(json.loads((folder/'status.json').read_text()).get('duration', 0))
    if not expected:
        import subprocess
        expected = float(json.loads(subprocess.check_output([
            str(ROOT/'bin/ffprobe'), '-v','error','-show_entries','format=duration',
            '-of','json',str(folder/'recap.mp4')]))['format']['duration'])
    matches = []
    for video in youtube.videos().list(part='snippet,contentDetails,status,fileDetails,processingDetails', id=','.join(ids)).execute().get('items', []):
        snippet = video['snippet']
        published = datetime.fromisoformat(snippet['publishedAt'].replace('Z','+00:00')).timestamp()
        match = re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?', video['contentDetails']['duration'])
        duration = sum(float(n or 0)*factor for n,factor in zip(match.groups(),[3600,60,1])) if match else -1
        same_media = (abs(duration-expected) <= 1.5 or
                      int(video.get('fileDetails',{}).get('fileSize',-1)) == (folder/'recap.mp4').stat().st_size)
        if (snippet.get('description','') == options['description'] + ('\n\n#Shorts' if '#Shorts' not in options['description'] and '#shorts' not in options['description'] else '')
                and same_media and since-300 <= published <= time.time()+60):
            matches.append(video)
    return matches[0] if len(matches)==1 else None

def upload(folder):
    result_file = folder/'publication.json'
    def record(**data):
        temp = result_file.with_suffix('.new')
        temp.write_text(json.dumps(data))
        temp.replace(result_file)
    previous = json.loads(result_file.read_text()) if result_file.exists() else {}
    if previous.get('status') == 'uploaded':
        return
    reconcile_only = previous.get('status') in {'uploading', 'unknown'}
    started_at = previous.get('started_at') or (result_file.stat().st_mtime if result_file.exists() else time.time())
    options = json.loads((folder/'request.json').read_text())['publication']
    if '#Shorts' not in options['description'] and '#shorts' not in options['description']:
        options['description'] += '\n\n#Shorts'
    token = ROOT/'data/youtube/token.json'
    if not token.exists():
        record(status='failed', message='Connect your YouTube channel in Clipper first.')
        return
    try:
        creds = Credentials.from_authorized_user_info(json.loads(token.read_text()))
        if not creds.valid:
            creds.refresh(Request())
    except Exception:
        record(status='failed', message='Reconnect your YouTube channel in Clipper, then retry.')
        return
    video = folder/'recap.mp4'
    if not video.is_file():
        record(status='failed', message='Render the recap first.')
        return
    # This marker is written before contacting YouTube. A killed worker leaves
    # an ambiguous result that cannot be blindly uploaded a second time.
    if not reconcile_only:
        started_at = time.time()
        record(status='uploading', progress=0, started_at=started_at, message='Uploading to YouTube')
    youtube = None
    try:
        youtube = build('youtube', 'v3', http=AuthorizedHttp(creds, http=httplib2.Http(timeout=60)), cache_discovery=False)
        if reconcile_only:
            existing = find_existing(youtube, options, folder, started_at)
            if existing:
                video_id = existing['id']
                record(status='uploaded', progress=100, video_id=video_id,
                    url='https://www.youtube.com/watch?v='+video_id,
                    privacy=existing.get('status',{}).get('privacyStatus', options['privacy']),
                    message='Uploaded to YouTube — confirmed. YouTube may still be processing the video.')
            else:
                record(status='unknown', started_at=started_at,
                    message='No unique matching upload found yet. Check again shortly or inspect YouTube Studio.')
            return
        req = youtube.videos().insert(part='snippet,status', body={
            'snippet': {'title':options['title'], 'description':options['description'] + ('\n\n#Shorts' if '#Shorts' not in options['description'] and '#shorts' not in options['description'] else ''), 'categoryId':'22', 'tags': ['Shorts', 'YouTubeShorts']},
            'status': {'privacyStatus':options['privacy'], 'selfDeclaredMadeForKids':options.get('made_for_kids',False)}
        }, media_body=MediaFileUpload(str(video), mimetype='video/mp4', resumable=True))
        response = None
        while response is None:
            progress, response = req.next_chunk(num_retries=2)
            if progress:
                record(status='uploading', started_at=started_at, progress=round(progress.progress()*100), message='Uploading to YouTube')
        video_id = response.get('id','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):
            raise ValueError('Unexpected upload response')
        record(status='uploaded', progress=100, video_id=video_id, url='https://www.youtube.com/watch?v='+video_id,
               privacy=options['privacy'], message='Published to YouTube')
    except HttpError as exc:
        # A quota/auth rejection is safe to retry; transport/5xx ambiguity is not.
        safe = exc.resp.status in {400,401,403,404,429}
        record(status='failed' if safe else 'unknown',
               message=('YouTube rejected the upload. Check channel authorization and upload quota before retrying.'
                        if safe else 'Upload result is uncertain. Check YouTube Studio before taking further action.'))
    except Exception as exc:
        existing = None
        if youtube:
            try:
                existing = find_existing(youtube, options, folder, started_at)
            except Exception:
                pass
        if existing:
            video_id = existing['id']
            record(status='uploaded', progress=100, video_id=video_id,
                url='https://www.youtube.com/watch?v='+video_id, privacy=options['privacy'],
                message='Uploaded to YouTube — confirmed. YouTube may still be processing the video.')
        else:
            record(status='unknown', started_at=started_at, error_type=type(exc).__name__,
                message='Confirmation was interrupted. Use Check YouTube upload to recover the result without uploading again.')
    finally:
        if youtube:
            youtube.close()


if __name__ == '__main__':
    os.umask(0o077)
    folder = Path(sys.argv[1]).resolve()
    if folder.parent != ROOT/'tools/narrated-dev/data/narrated/jobs' or not re.fullmatch('[a-f0-9]{32}',folder.name):
        raise SystemExit('Invalid narrated job')
    upload(folder)
