from __future__ import annotations

import io
import json
import unittest
import urllib.error

import support  # noqa: F401 — adds backend to sys.path

import anthropic_client


class RequestModelCachingTests(unittest.TestCase):
    def test_body_carries_cache_control_and_optional_schema(self) -> None:
        captured: dict = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(
                    {
                        "content": [{"type": "text", "text": "привет"}],
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    }
                ).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Resp()

        orig = anthropic_client.urllib.request.urlopen
        self.addCleanup(lambda: setattr(anthropic_client.urllib.request, "urlopen", orig))
        anthropic_client.urllib.request.urlopen = fake_urlopen

        text, _usage = anthropic_client._request_model(
            "system text",
            "user text",
            schema=None,
            model="m",
            max_tokens=10,
            api_key="k",
            timeout=1,
        )
        self.assertEqual(text, "привет")
        body = captured["body"]
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        first_content = body["messages"][0]["content"]
        self.assertEqual(first_content[0]["cache_control"], {"type": "ephemeral"})
        # Без схемы формат не задаётся; effort живёт в том же output_config и
        # к схеме отношения не имеет.
        self.assertNotIn("format", body.get("output_config", {}))

        anthropic_client._request_model(
            "system text",
            "user text",
            schema={"type": "object"},
            model="m",
            max_tokens=10,
            api_key="k",
            timeout=1,
        )
        self.assertIn("format", captured["body"]["output_config"])

    def test_effort_is_sent_when_configured(self) -> None:
        captured: dict = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(
                    {
                        "content": [{"type": "text", "text": "ок"}],
                        "usage": {},
                    }
                ).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Resp()

        orig = anthropic_client.urllib.request.urlopen
        self.addCleanup(lambda: setattr(anthropic_client.urllib.request, "urlopen", orig))
        anthropic_client.urllib.request.urlopen = fake_urlopen

        orig_effort = anthropic_client.DEFAULT_EFFORT
        self.addCleanup(lambda: setattr(anthropic_client, "DEFAULT_EFFORT", orig_effort))

        anthropic_client.DEFAULT_EFFORT = "medium"
        anthropic_client._request_model(
            "s",
            "u",
            schema=None,
            model="m",
            max_tokens=10,
            api_key="k",
            timeout=1,
        )
        self.assertEqual(captured["body"]["output_config"]["effort"], "medium")

        # Пустая строка — способ вообще не слать effort (например, на модели,
        # где уровень недоступен).
        anthropic_client.DEFAULT_EFFORT = ""
        anthropic_client._request_model(
            "s",
            "u",
            schema=None,
            model="m",
            max_tokens=10,
            api_key="k",
            timeout=1,
        )
        self.assertNotIn("output_config", captured["body"])

    def test_max_tokens_stop_reason_names_the_budget(self) -> None:
        """Мышление тратит тот же max_tokens, поэтому обрезанный ответ должен
        жаловаться на бюджет, а не выглядеть как сломанная схема."""

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(
                    {
                        "stop_reason": "max_tokens",
                        "content": [{"type": "text", "text": '{"focus": "нез'}],
                        "usage": {},
                    }
                ).encode("utf-8")

        orig = anthropic_client.urllib.request.urlopen
        self.addCleanup(lambda: setattr(anthropic_client.urllib.request, "urlopen", orig))
        anthropic_client.urllib.request.urlopen = lambda request, timeout=None: _Resp()

        with self.assertRaises(anthropic_client.RecommendationError) as ctx:
            anthropic_client._request_model(
                "s",
                "u",
                schema={"type": "object"},
                model="m",
                max_tokens=777,
                api_key="k",
                timeout=1,
            )
        self.assertIn("777", str(ctx.exception))
        self.assertIn("ANTHROPIC_MAX_TOKENS", str(ctx.exception))


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "msg", None, io.BytesIO(b"detail"))


class FetchRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = anthropic_client.urllib.request.urlopen
        self.addCleanup(lambda: setattr(anthropic_client.urllib.request, "urlopen", self._orig))
        self.slept: list[float] = []

    def _fetch(self, max_retries: int = 2):
        return anthropic_client._fetch_anthropic(
            object(),
            timeout=1,
            max_retries=max_retries,
            backoff=0.5,
            sleep=self.slept.append,
        )

    def _patch(self, sequence) -> list[int]:
        calls = {"n": 0}
        it = iter(sequence)

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            item = next(it)
            if isinstance(item, Exception):
                raise item
            return _FakeResponse(item)

        anthropic_client.urllib.request.urlopen = fake_urlopen
        return calls

    def test_retries_transient_then_succeeds(self) -> None:
        calls = self._patch([_http_error(503), b"ok"])
        self.assertEqual(self._fetch(), "ok")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.slept, [0.5])  # one backoff before the 2nd try

    def test_permanent_error_is_not_retried(self) -> None:
        calls = self._patch([_http_error(400)])
        with self.assertRaises(anthropic_client.RecommendationError):
            self._fetch()
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.slept, [])

    def test_exhausts_retries_on_persistent_transient(self) -> None:
        calls = self._patch([_http_error(529), _http_error(529), _http_error(529)])
        with self.assertRaisesRegex(anthropic_client.RecommendationError, "529"):
            self._fetch(max_retries=2)
        self.assertEqual(calls["n"], 3)  # initial + 2 retries
        self.assertEqual(self.slept, [0.5, 1.0])  # exponential backoff

    def test_url_error_retried_then_raised(self) -> None:
        calls = self._patch([urllib.error.URLError("conn reset"), b"ok"])
        self.assertEqual(self._fetch(), "ok")
        self.assertEqual(calls["n"], 2)
