"""Start des Webservers mit automatischer Wahl eines freien Ports.

Ab ``BORA_PORT`` (Standard 8088) werden bis zu ``BORA_PORT_SEARCH`` Ports
(Standard 100) aufsteigend probiert; der erste freie wird genommen. Der Socket
wird hier gebunden und an uvicorn übergeben - kein Zeitfenster, in dem ein
anderer Prozess den Port "wegschnappen" könnte.

Ports aus ``BORA_PORT_EXCLUDE`` (Standard ``8080,8090``; 8090 = BORA-Anwendung)
werden nie verwendet - auch dann nicht, wenn sie gerade frei sind.

Sinnvoll mit ``--network host``: dann sieht der Container die Ports des Hosts.
Der gewählte Port steht in ``docker logs`` und in /tmp/bora-port (Healthcheck).
"""
from __future__ import annotations

import logging
import os
import socket
import sys

import uvicorn

PORT_FILE = "/tmp/bora-port"
DEFAULT_EXCLUDE = "8080,8090"  # 8090 ist für die BORA-Anwendung reserviert


def parse_ports(spec: str) -> set[int]:
    """'8080,8090,9000-9010' -> Menge von Ports."""
    ports: set[int] = set()
    for part in spec.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            ports.update(range(lo, hi + 1))
        else:
            ports.add(int(part))
    return ports
log = logging.getLogger("bora.serve")


def bind_free_port(start: int, attempts: int, host: str = "0.0.0.0",
                   exclude: frozenset[int] | set[int] = frozenset()) -> socket.socket:
    last_error: OSError | str | None = None
    for port in range(start, start + max(attempts, 1)):
        if port in exclude:
            last_error = f"Port {port} ist reserviert (BORA_PORT_EXCLUDE)"
            print(f"Port {port} reserviert – übersprungen", file=sys.stderr, flush=True)
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            sock.listen(2048)
            sock.set_inheritable(True)
            return sock
        except OSError as exc:
            sock.close()
            last_error = exc
            if attempts > 1:
                print(f"Port {port} belegt – nächster …", file=sys.stderr, flush=True)
    raise SystemExit(f"Kein freier Port im Bereich {start}–{start + attempts - 1}: {last_error}")


def main() -> None:
    start = int(os.environ.get("BORA_PORT", "8088"))
    attempts = int(os.environ.get("BORA_PORT_SEARCH", "100"))
    exclude = parse_ports(os.environ.get("BORA_PORT_EXCLUDE", DEFAULT_EXCLUDE))
    sock = bind_free_port(start, attempts, exclude=exclude)
    port = sock.getsockname()[1]
    os.environ["BORA_PORT"] = str(port)  # für Start-Diagnose/Banner
    try:
        with open(PORT_FILE, "w") as fh:
            fh.write(str(port))
    except OSError:
        pass
    print(f"BORA Rohdaten-Optimizer lauscht auf Port {port}  →  http://<host>:{port}", flush=True)
    config = uvicorn.Config("app.main:create_app", factory=True, proxy_headers=True,
                            forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
