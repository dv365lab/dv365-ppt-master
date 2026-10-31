#!/usr/bin/env python3
"""PPT Master - Web GUI Port Tests

Exercise startup with an occupied preferred port.

Usage:
    python3 -m unittest tests.test_web_gui_port

Dependencies:
    flask
"""

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1]


class WebGuiPortTests(unittest.TestCase):
    def test_falls_back_when_port_is_taken_after_probe(self):
        from web_gui.server import _bind_server

        with patch("web_gui.server.find_free_port", return_value=8765), \
             patch("werkzeug.serving.make_server", side_effect=[SystemExit(1), "server"]) as make:
            self.assertEqual(_bind_server(object(), 8765), "server")
            self.assertEqual(make.call_args_list[1].args[1], 0)

    def test_falls_back_when_probe_range_is_exhausted(self):
        from web_gui.server import _bind_server

        with patch("web_gui.server.find_free_port", side_effect=RuntimeError), \
             patch("werkzeug.serving.make_server", return_value="server") as make:
            self.assertEqual(_bind_server(object(), 8765), "server")
            self.assertEqual(make.call_args.args[1], 0)

    def test_occupied_port_starts_server_on_another_port(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            process = subprocess.Popen(
                [sys.executable, str(SCRIPTS_DIR / "web_gui/server.py"), "--no-browser",
                 "--projects-dir", directory, "--port", str(port)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                lock = Path(directory) / ".web_gui/server.lock"
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not lock.exists() and process.poll() is None:
                    time.sleep(0.05)
                self.assertTrue(lock.exists(), "server did not start")
                info = json.loads(lock.read_text(encoding="utf-8"))
                self.assertNotEqual(info["port"], port)
                with urllib.request.urlopen(f'http://127.0.0.1:{info["port"]}/api/health', timeout=3) as response:
                    self.assertEqual(json.load(response)["service"], "ppt-master-web-gui")
            finally:
                process.terminate()
                process.communicate(timeout=10)
