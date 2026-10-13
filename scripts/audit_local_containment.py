"""Read-only path/link audit for the macOS project-local runtime."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import config


def inside(path):
    return Path(path).resolve().is_relative_to(ROOT)


def audit():
    errors = []
    paths = {}
    for name in ['PROJECT_ROOT', 'DATA_ROOT', 'DATA_MODELS', 'DATA_CACHE', 'DATA_TEMP',
                 'DATA_CONFIG', 'DATA_LOCAL', 'DATA_OUTPUT', 'DATA_INPUT', 'DATA_SOURCES',
                 'DATA_JOBS', 'DATA_THUMBNAILS', 'DATA_DB', 'DATA_YOUTUBE', 'PROJECT_LOGS',
                 'PROJECT_TOOLS']:
        path = getattr(config, name)
        paths[name] = str(path.resolve())
        if not inside(path):
            errors.append(f'{name} resolves outside PROJECT_ROOT')
    for relative in ['.venv', 'bin', 'tools', '.cache', '.models', 'data', 'logs']:
        if not inside(ROOT / relative):
            errors.append(f'{relative} resolves outside PROJECT_ROOT')
    binaries = {}
    for name, path in [('ffmpeg', config.get_ffmpeg_path()), ('ffprobe', config.get_ffprobe_path()),
                       ('deno', ROOT / 'bin/deno')]:
        path = Path(path)
        if not inside(path) or not path.is_file() or not os.access(path, os.X_OK):
            errors.append(f'{name} is missing, external, or not executable')
            continue
        item = {'path': str(path.resolve())}
        if sys.platform == 'darwin':
            result = subprocess.run(['/usr/bin/otool', '-L', str(path)], capture_output=True, text=True)
            if result.returncode:
                errors.append(f'Cannot inspect {name} linked libraries')
            deps = [line.strip().split(' (', 1)[0] for line in result.stdout.splitlines()[1:]]
            item['libraries'] = deps
            for dep in deps:
                if not dep.startswith(('/usr/lib/', '/System/Library/')) and not inside(dep):
                    errors.append(f'{name}: external or unresolved library {dep}')
        binaries[name] = item
    for key in ['TMPDIR', 'TMP', 'TEMP', 'PIP_CACHE_DIR', 'DENO_DIR', 'XDG_CACHE_HOME',
                'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'HF_HOME', 'TORCH_HOME']:
        if not os.environ.get(key) or not inside(os.environ[key]):
            errors.append(f'{key} is not contained')
    return {'project_root': str(ROOT), 'paths': paths, 'binaries': binaries,
            'errors': errors, 'result': 'FAIL' if errors else 'PASS'}


if __name__ == '__main__':
    result = audit()
    print(json.dumps(result, indent=2))
    raise SystemExit(bool(result['errors']))
