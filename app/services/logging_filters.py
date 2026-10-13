"""Keep successful browser polling out of the access log, retaining failures."""
import logging


class StatusFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn: client, method, path, HTTP version, status code.
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            _, method, path, _, status = args
            if method == "GET" and str(path).startswith("/api/status/") and str(status) == "200":
                return False
        return True
