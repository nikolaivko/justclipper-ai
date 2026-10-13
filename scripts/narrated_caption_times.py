"""Local word timing for narrated audio; no model downloads or cloud requests."""
import json
import sys
from pathlib import Path
from faster_whisper import WhisperModel

ROOT=Path(__file__).resolve().parents[1]
folder=Path(sys.argv[1]).resolve()
if folder.parent != ROOT/'tools/narrated-dev/data/narrated/jobs':
    raise SystemExit('Invalid narrated folder')
snapshot=next((ROOT/'.models/whisper/models--Systran--faster-whisper-small/snapshots').iterdir())
model=WhisperModel(str(snapshot),device='cpu',compute_type='int8',local_files_only=True)
manifest=json.loads((folder/'caption-audio.json').read_text())
result=[]
for entry in manifest:
    path=(folder/entry['file']).resolve()
    if path.parent!=folder:raise SystemExit('Invalid audio path')
    segments,_=model.transcribe(str(path),language='en',word_timestamps=True,vad_filter=True,
        initial_prompt=entry['text'])
    result.append([{'word':w.word.strip(),'start':w.start,'end':w.end} for s in segments for w in s.words or []])
(folder/'caption-timings.json').write_text(json.dumps(result))
