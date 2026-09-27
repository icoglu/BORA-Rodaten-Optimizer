import socket

from app.serve import bind_free_port


def test_skips_busy_port():
    busy = socket.socket()
    busy.bind(("0.0.0.0", 0))
    busy.listen(1)
    start = busy.getsockname()[1]
    try:
        sock = bind_free_port(start, attempts=20)
        try:
            assert sock.getsockname()[1] > start
        finally:
            sock.close()
    finally:
        busy.close()


def test_reserved_port_never_used():
    from app.serve import parse_ports
    assert parse_ports("8080, 8090;9000-9002") == {8080, 8090, 9000, 9001, 9002}
    probe = socket.socket()
    probe.bind(("0.0.0.0", 0))
    free = probe.getsockname()[1]
    probe.close()
    sock = bind_free_port(free, attempts=5, exclude={free})  # frei, aber reserviert
    try:
        assert sock.getsockname()[1] != free
    finally:
        sock.close()
