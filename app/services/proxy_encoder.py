"""Bounded proxy encoding with live FFmpeg timestamps and retained diagnostics."""
import queue
import subprocess
import threading
import time

def encode_proxy_command(command, duration, callback, active_processes, log_path, stall_seconds=180):
    updates = queue.Queue()
    with log_path.open("w") as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=errors, text=True, bufsize=1)
        active_processes.add(process)

        def read_progress():
            try:
                for line in process.stdout:
                    updates.put(line)
            finally:
                updates.put(None)

        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        last_advance = time.monotonic()
        last_seconds = -1
        stalled = False
        try:
            while True:
                try:
                    line = updates.get(timeout=1)
                except queue.Empty:
                    line = ""
                if line is None:
                    break
                if line.startswith("out_time_us="):
                    try:
                        seconds = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    if seconds > last_seconds:
                        last_seconds = seconds
                        last_advance = time.monotonic()
                        if callback and duration > 0:
                            callback(min(.99, max(0, seconds / duration)), "Preparing video for analysis")
                if time.monotonic() - last_advance > stall_seconds:
                    stalled = True
                    process.terminate()
                    break
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join(timeout=2)
            process.stdout.close()
            active_processes.discard(process)
    if stalled:
        with log_path.open("a") as errors:
            errors.write(f"\nProxy encoder made no timestamp progress for {stall_seconds:g} seconds.\n")
    return process.returncode if not stalled else -1
