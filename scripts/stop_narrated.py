"""Gracefully stop only this checkout's standalone Narrated server."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / 'tools/narrated-dev'


def candidates():
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,command='],
                            check=True, capture_output=True, text=True)
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        pid, command = fields
        words = command.split()
        old_entry = any(words[i:i+3] == ['-m', 'uvicorn', 'app.narrated_main:app']
                        for i in range(len(words)-2))
        new_entry = any(words[i:i+2] == ['-m', 'app.narrated_server']
                        for i in range(len(words)-1))
        if not (old_entry or new_entry):
            continue
        # Do not stop other checkouts or unrelated services using the same port.
        cwd = subprocess.run(['/usr/sbin/lsof', '-a', '-p', pid, '-d', 'cwd', '-Fn'],
                             capture_output=True, text=True)
        if f'n{DEV}' in cwd.stdout.splitlines():
            yield int(pid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        pids = list(candidates())
    except subprocess.CalledProcessError:
        print('Could not inspect running processes; nothing was stopped.')
        return 1
    if not pids:
        print('Narrated is not running. Clipper was not changed.')
        return 0
    if args.dry_run:
        print('Would gracefully stop Narrated PID(s): ' + ', '.join(map(str, pids)))
        return 0
    print('Stopping Narrated and cleaning its project files...')
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not set(pids).intersection(candidates()):
            print('Narrated stopped. Saved voices and Clipper are unchanged.')
            return 0
        time.sleep(.5)
    print('Narrated is still shutting down. No forced termination was used.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
