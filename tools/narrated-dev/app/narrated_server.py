"""Reserve the port before starting any project lifecycle/cleanup hooks."""
import socket
import uvicorn


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(('127.0.0.1', 8010))
        except OSError:
            print('Port 8010 is already in use. No projects were changed. Open http://127.0.0.1:8010')
            return 1
        listener.listen(128)
        server = uvicorn.Server(uvicorn.Config('app.narrated_main:app', host='127.0.0.1', port=8010, access_log=False))
        server.run(sockets=[listener])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
