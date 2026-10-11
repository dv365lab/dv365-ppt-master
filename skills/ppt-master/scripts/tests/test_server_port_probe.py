#!/usr/bin/env python3
"""PPT Master - Local Port Probe Tests

Check that a reusable Windows listener cannot be mistaken for a free port.

Usage:
    python3 -m unittest tests.test_server_port_probe

Dependencies:
    None (only uses standard library)
"""

import socket
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from server_common import find_free_port  # noqa: E402


class PortProbeTests(unittest.TestCase):
    def test_reusable_listener_is_not_free(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaises(RuntimeError):
                find_free_port(port, span=1)

    def test_invalid_port_rejected(self):
        with self.assertRaises(ValueError):
            find_free_port(0)
