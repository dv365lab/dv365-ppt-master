#!/usr/bin/env python3
"""PPT Master - Provider Tests

Verify compatible chat requests, bounded retries and redacted failures.

Usage:
    python3 -m unittest tests.test_web_gui_provider

Dependencies:
    requests
"""

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from web_gui.provider import ModelClient, ProviderConfig, ProviderError  # noqa: E402


def response(status=200, content='{"ok":true}', finish_reason="stop"):
    result = Mock(status_code=status)
    result.json.return_value = {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}
    return result


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.config = ProviderConfig("test-secret-value", "https://provider.example/v1", "agnes-2.5-flash")
        self.session = Mock()
        self.cancel = threading.Event()
        self.client = ModelClient(self.config, session=self.session, cancel_event=self.cancel)

    def test_chat_schema_and_no_credential_in_body(self):
        self.assertNotIn("test-secret-value", repr(self.config))
        self.session.post.return_value = response()
        self.assertEqual(self.client.complete("system", "input"), '{"ok":true}')
        args, kwargs = self.session.post.call_args
        self.assertEqual(args[0], "https://provider.example/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-secret-value")
        self.assertNotIn("test-secret-value", str(kwargs["json"]))
        self.assertFalse(kwargs["allow_redirects"])

    def test_retry_transient_error(self):
        self.session.post.side_effect = [response(429), response()]
        with patch.object(self.cancel, "wait", return_value=False):
            self.assertEqual(self.client.complete("system", "input"), '{"ok":true}')
        self.assertEqual(self.session.post.call_count, 2)

    def test_rate_limit_honors_bounded_retry_after(self):
        limited = response(429)
        limited.headers = {"Retry-After": "8"}
        self.session.post.side_effect = [limited, response()]
        with patch.object(self.cancel, "wait", return_value=False) as wait:
            self.client.complete("system", "input")
        wait.assert_called_once_with(8.0)

    def test_rate_limit_without_header_waits_for_a_window(self):
        limited = response(429)
        limited.headers = {}
        self.session.post.side_effect = [limited, response()]
        with patch.object(self.cancel, "wait", return_value=False) as wait:
            self.client.complete("system", "input")
        wait.assert_called_once_with(20.0)

    def test_json_mode_only_for_structured_calls(self):
        self.session.post.return_value = response()
        self.client.complete("JSON", "input", json_mode=True)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["response_format"], {"type": "json_object"})
        self.client.complete("SVG", "input")
        self.assertNotIn("response_format", self.session.post.call_args.kwargs["json"])

    def test_auth_failure_is_not_retried_or_echoed(self):
        self.session.post.return_value = response(401, "test-secret-value")
        with self.assertRaises(ProviderError) as caught:
            self.client.complete("system", "input")
        self.assertNotIn("test-secret-value", str(caught.exception))
        self.assertEqual(self.session.post.call_count, 1)

    def test_redirect_is_rejected(self):
        self.session.post.return_value = response(302)
        with self.assertRaises(ProviderError):
            self.client.complete("system", "input")

    def test_truncation_and_empty_response_fail(self):
        for reply in (response(content="", finish_reason="stop"), response(finish_reason="length")):
            self.session.post.return_value = reply
            with self.assertRaises(ProviderError):
                self.client.complete("system", "input")

    def test_cancellation_does_not_send_request(self):
        self.cancel.set()
        with self.assertRaises(InterruptedError):
            self.client.complete("system", "input")
        self.session.post.assert_not_called()
