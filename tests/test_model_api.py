from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from my_superagent.config import ConfigurationError, ModelConfig
from my_superagent.llm import ModelAPIError, ModelClient


class ConfigTests(unittest.TestCase):
    def test_file_config_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                '# local settings\nSUPERAGENT_API_KEY="file-secret"\n'
                'SUPERAGENT_BASE_URL=https://example.test/v1/\n'
                'SUPERAGENT_MODEL=file-model\n', encoding="utf-8",
            )
            config = ModelConfig.load(path, {"SUPERAGENT_MODEL": "env-model"})
            self.assertEqual(config.api_key, "file-secret")
            self.assertEqual(config.base_url, "https://example.test/v1")
            self.assertEqual(config.model, "env-model")
            self.assertNotIn("file-secret", repr(config))

    def test_invalid_configuration(self):
        for kwargs in ({"api_key": ""}, {"api_key": "test", "timeout_seconds": float("nan")},
                       {"api_key": "test", "base_url": "https://user:secret@example.test/v1"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigurationError):
                ModelConfig(**kwargs)


class ModelClientTests(unittest.TestCase):
    def test_responses_request_and_output(self):
        client = ModelClient(ModelConfig("test-secret", base_url="https://example.test/v1"))
        body = {
            "status": "completed", "model": "gpt-6.1-terra",
            "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "content": [{"type": "output_text", "text": "OK"}]},
            ],
            "usage": {"total_tokens": 10},
        }
        with patch.object(client._opener, "open", return_value=io.BytesIO(json.dumps(body).encode())) as send:
            reply = client.generate("test prompt")
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.test/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(json.loads(request.data)["input"], "test prompt")
        self.assertFalse(json.loads(request.data)["store"])
        self.assertEqual(reply.text, "OK")

    def test_chat_completions_mode(self):
        client = ModelClient(ModelConfig("test-secret", api_mode="chat_completions"))
        body = {"choices": [{"message": {"content": "OK"}}]}
        with patch.object(client._opener, "open", return_value=io.BytesIO(json.dumps(body).encode())) as send:
            self.assertEqual(client.generate("ping").text, "OK")
        self.assertTrue(send.call_args.args[0].full_url.endswith("/chat/completions"))

    def test_http_error_omits_response_body_and_key(self):
        client = ModelClient(ModelConfig("test-secret"))
        error = HTTPError("https://example.test", 401, "test-secret", {}, io.BytesIO(b"test-secret"))
        with patch.object(client._opener, "open", side_effect=error):
            with self.assertRaises(ModelAPIError) as caught:
                client.generate("ping")
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn("test-secret", str(caught.exception))

    def test_rejects_empty_or_invalid_response(self):
        for body in (b"not-json", b"{}", b'{"status":"failed"}', b'[]'):
            with self.subTest(body=body):
                client = ModelClient(ModelConfig("test-secret"))
                with patch.object(client._opener, "open", return_value=io.BytesIO(body)):
                    with self.assertRaises(ModelAPIError):
                        client.generate("ping")


if __name__ == "__main__":
    unittest.main()
