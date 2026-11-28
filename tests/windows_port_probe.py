"""Try to bind 127.0.0.1:PORT beside the program listening there, the way an impostor would
(SO_REUSEADDR set). Exit 0 when that is refused, 1 when the second bind succeeds.

    python tests/windows_port_probe.py PORT
"""
import socket
import sys


def second_bind_refused(port):
    """The error that refused a second SO_REUSEADDR bind on 127.0.0.1:``port``, or '' if it bound."""
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
        except OSError as error:
            return str(error)
    return ''


if __name__ == '__main__':
    port = int(sys.argv[1])
    refused = second_bind_refused(port)
    print(f'A second SO_REUSEADDR bind on 127.0.0.1:{port} ' + (f'was refused ({refused}).' if refused else 'SUCCEEDED.'))
    sys.exit(0 if refused else 1)
