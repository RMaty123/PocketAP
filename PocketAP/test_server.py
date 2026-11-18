"""Testovací HTTP a TCP echo server pro ověření komunikace klient - hostitel.

Samostatné spuštění:
    python test_server.py --bind 192.168.137.1
"""
from __future__ import annotations

import argparse
import html
import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from logger import get_logger, setup_logging

log = get_logger("testserver")


class _HttpHandler(BaseHTTPRequestHandler):
    server_version = "PocketAP-Test"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - název určuje http.server
        client_ip = self.client_address[0]
        log.info("HTTP požadavek od %s: %s", client_ip, self.path.split("?")[0])
        if self.path.startswith("/ping"):
            body = json.dumps({"status": "ok", "client": client_ip}).encode("utf-8")
            content_type = "application/json"
        else:
            body = self._page(client_ip).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _page(client_ip: str) -> str:
        stamp = time.strftime("%H:%M:%S")
        return (
            "<!doctype html><html lang='cs'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>PocketAP test</title></head>"
            "<body style='font-family:sans-serif;margin:2rem;text-align:center'>"
            "<h1 style='color:#15803d'>Spojení funguje</h1>"
            f"<p>Vaše IP adresa: <b>{html.escape(client_ip)}</b></p>"
            f"<p>Čas na hostiteli: <b>{stamp}</b></p></body></html>"
        )

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        log.debug("HTTP: " + format, *args)


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        log.info("TCP echo: připojen %s", self.client_address[0])
        self.request.settimeout(60)
        try:
            while data := self.request.recv(4096):
                self.request.sendall(data)
        except OSError as exc:
            log.debug("TCP echo: %s", exc)


class _ThreadingHttp(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


class _ThreadingEcho(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True


class LocalTestServer:
    """Spravuje oba testovací servery navázané jen na adresu AP."""

    def __init__(self, bind_ip: str, http_port: int = 8080, echo_port: int = 9000) -> None:
        self.bind_ip = bind_ip
        self.http_port = http_port
        self.echo_port = echo_port
        self._servers: list[socketserver.BaseServer] = []
        self._threads: list[threading.Thread] = []

    @property
    def http_url(self) -> str:
        return f"http://{self.bind_ip}:{self.http_port}/"

    @property
    def echo_endpoint(self) -> str:
        return f"{self.bind_ip}:{self.echo_port}"

    def start(self) -> None:
        """Spustí servery. Při chybě uvolní vše, co už bylo otevřené."""
        try:
            self._servers = [
                _ThreadingHttp((self.bind_ip, self.http_port), _HttpHandler),
                _ThreadingEcho((self.bind_ip, self.echo_port), _EchoHandler),
            ]
        except OSError:
            self.stop()
            raise
        for server in self._servers:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("Testovací server: %s, TCP echo %s", self.http_url, self.echo_endpoint)

    def stop(self) -> None:
        """Zastaví servery a uvolní porty."""
        running = bool(self._threads)
        for server in self._servers:
            if running:
                server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)
        self._servers.clear()
        self._threads.clear()


def main() -> int:
    parser = argparse.ArgumentParser(description="Testovací server PocketAP")
    parser.add_argument("--bind", required=True, help="IP adresa rozhraní AP, například 192.168.137.1")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--echo-port", type=int, default=9000)
    args = parser.parse_args()

    setup_logging()
    server = LocalTestServer(args.bind, args.http_port, args.echo_port)
    server.start()
    print("Běží. Ukončíte klávesami Ctrl+C.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
