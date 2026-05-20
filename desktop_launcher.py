from __future__ import annotations

import multiprocessing
import os
import socket
import threading
import webbrowser

import uvicorn


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _find_available_port(start_port: int) -> int:
    for port in range(start_port, start_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            try:
                sock.bind((DEFAULT_HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError("No available local port was found.")


def _open_browser(url: str) -> None:
    webbrowser.open(url, new=2)


def main() -> None:
    multiprocessing.freeze_support()
    os.environ.setdefault("APP_ENV", "desktop")
    os.environ.setdefault("REQUEUE_PROCESSING_OCR_ON_STARTUP", "true")

    port = _find_available_port(int(os.getenv("APP_PORT", str(DEFAULT_PORT))))
    url = f"http://{DEFAULT_HOST}:{port}"
    threading.Timer(1.2, _open_browser, args=(url,)).start()
    print(f"Transfer Summary Tool is running: {url}")
    print("Close this window to stop the application.")
    uvicorn.run("app.main:app", host=DEFAULT_HOST, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
