"""OpenAISupervisorAdapter con transporte falso: nunca hay red real."""
import json

import pytest

from factory.adapters.mock import scenario_verdict
from factory.adapters.openai_adapter import OpenAISupervisorAdapter
from factory.config import load_config
from factory.errors import SupervisorUnavailable

KEY = "sk-test-" + "k" * 40
PACKET = {"review": {"gate": "G6"}, "project": {"commit": "a" * 40}, "packet_kind": "INITIAL"}


def cfg(**over):
    base = {"SUPERVISOR_PROVIDER": "openai", "SUPERVISOR_LIVE_ENABLED": True, "SUPERVISOR_MODEL": "modelo-configurado",
            "SUPERVISOR_BACKOFF_BASE_S": 0, "SUPERVISOR_BACKOFF_MAX_S": 0}
    base.update(over)
    return load_config(env={}, overrides=base)


def ok_body(text, usage=None, status="completed"):
    return json.dumps({"id": "resp_1", "model": "modelo-configurado-2026", "status": status,
                       "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                       "usage": usage or {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 200}, "output_tokens": 300,
                                          "output_tokens_details": {"reasoning_tokens": 100}, "total_tokens": 1300}}).encode()


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, body, headers, timeout):
        self.calls.append({"method": method, "url": url, "body": json.loads(body) if body else None, "headers": headers, "timeout": timeout})
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)


def adapter(t, **over):
    sleeps = []
    a = OpenAISupervisorAdapter(cfg(**over), transport=t, sleep=sleeps.append)
    a.sleeps = sleeps
    return a


def test_request_shape_matches_responses_api():
    t = FakeTransport((200, {}, ok_body(json.dumps(scenario_verdict("PASS", PACKET)))))
    r = adapter(t, SUPERVISOR_REASONING_LEVEL="high", SUPERVISOR_MAX_OUTPUT=12345).review(PACKET)
    assert r.status == "OK" and r.verdict["verdict"] == "PASS" and r.model == "modelo-configurado-2026"
    c = t.calls[0]
    assert c["method"] == "POST" and c["url"] == "https://api.openai.com/v1/responses"
    b = c["body"]
    assert b["model"] == "modelo-configurado" and b["store"] is False and b["max_output_tokens"] == 12345
    assert b["reasoning"] == {"effort": "high"}
    assert "Independent Principal Software Engineer" in b["instructions"]
    assert b["input"][0]["role"] == "user" and "untrusted project DATA" in b["input"][0]["content"]
    fmt = b["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and fmt["name"] == "supervisor_verdict" and "$schema" not in fmt["schema"]
    assert c["headers"]["Authorization"] == "Bearer " + KEY  # solo en la cabecera de la petición
    u = r.calls[0]["usage"]
    assert u == {"input_tokens": 1000, "cached_tokens": 200, "output_tokens": 300, "reasoning_tokens": 100, "total_tokens": 1300}


def test_reasoning_omitted_when_not_configured():
    t = FakeTransport((200, {}, ok_body(json.dumps(scenario_verdict("PASS", PACKET)))))
    adapter(t).review(PACKET)
    assert "reasoning" not in t.calls[0]["body"]


def test_live_disabled_never_touches_network():
    t = FakeTransport((200, {}, b"{}"))
    r = adapter(t, SUPERVISOR_LIVE_ENABLED=False).review(PACKET)
    assert r.status == "SUPERVISOR_PENDING" and t.calls == []
    assert adapter(t, SUPERVISOR_LIVE_ENABLED=False).health_check()["ok"] is False and t.calls == []


def test_missing_model_is_pending():
    t = FakeTransport((200, {}, b"{}"))
    assert adapter(t, SUPERVISOR_MODEL="").review(PACKET).status == "SUPERVISOR_PENDING" and t.calls == []


def test_missing_key_is_unavailable_without_retry(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    t = FakeTransport((200, {}, b"{}"))
    r = adapter(t).review(PACKET)
    assert r.status == "SUPERVISOR_UNAVAILABLE" and t.calls == []


def test_rate_limit_retries_with_backoff_then_succeeds():
    t = FakeTransport((429, {"Retry-After": "7"}, b'{"error":"rate"}'), (200, {}, ok_body(json.dumps(scenario_verdict("PASS", PACKET)))))
    a = adapter(t, SUPERVISOR_BACKOFF_MAX_S=10)
    r = a.review(PACKET)
    assert r.status == "OK" and len(t.calls) == 2 and 7 <= a.sleeps[0] <= 7.3


def test_5xx_exhausts_retries_then_unavailable():
    t = FakeTransport((503, {}, b"down"))
    a = adapter(t, SUPERVISOR_MAX_RETRIES=2)
    r = a.review(PACKET)
    assert r.status == "SUPERVISOR_UNAVAILABLE" and len(t.calls) == 3 and len(a.sleeps) == 2


def test_timeout_is_unavailable():
    t = FakeTransport(SupervisorUnavailable("TIMEOUT tras 300s"))
    r = adapter(t, SUPERVISOR_MAX_RETRIES=1).review(PACKET)
    assert r.status == "SUPERVISOR_UNAVAILABLE" and "TIMEOUT" in r.detail and len(t.calls) == 2


def test_auth_error_not_retried_and_key_not_leaked():
    t = FakeTransport((401, {}, json.dumps({"error": f"bad key {KEY}"}).encode()))
    r = adapter(t).review(PACKET)
    assert r.status == "SUPERVISOR_UNAVAILABLE" and len(t.calls) == 1
    assert KEY not in r.detail and KEY not in json.dumps(r.calls)


def test_incomplete_is_invalid_without_repair():
    t = FakeTransport((200, {}, json.dumps({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}).encode()))
    r = adapter(t).review(PACKET)
    assert r.status == "INVALID_SUPERVISOR_RESPONSE" and "max_output_tokens" in r.detail and len(t.calls) == 1


def test_refusal_is_invalid():
    body = json.dumps({"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}).encode()
    r = adapter(FakeTransport((200, {}, body))).review(PACKET)
    assert r.status == "INVALID_SUPERVISOR_RESPONSE"


def test_invalid_json_gets_one_repair_with_errors():
    t = FakeTransport((200, {}, ok_body("PASS, todo bien")), (200, {}, ok_body(json.dumps(scenario_verdict("PASS", PACKET)))))
    r = adapter(t).review(PACKET)
    assert r.status == "OK" and len(r.calls) == 2
    inp = t.calls[1]["body"]["input"]
    assert inp[1] == {"role": "assistant", "content": "PASS, todo bien"} and "did not match" in inp[2]["content"]


def test_ambiguous_text_never_becomes_pass():
    t = FakeTransport((200, {}, ok_body("PASS")))
    r = adapter(t).review(PACKET)
    assert r.verdict is None and r.status == "INVALID_SUPERVISOR_RESPONSE"


def test_health_check_uses_models_endpoint():
    t = FakeTransport((200, {}, b'{"id":"modelo-configurado"}'))
    h = adapter(t).health_check()
    assert h["ok"] and t.calls[0]["method"] == "GET" and t.calls[0]["url"].endswith("/models/modelo-configurado")


def test_cost_only_with_configured_prices(project, draft):
    from factory.review import _sum_usage
    calls = [{"model": "m", "usage": {"input_tokens": 1_000_000, "cached_tokens": 0, "output_tokens": 0}}]
    assert _sum_usage(calls, None)["cost_estimate"] is None
    c = load_config(env={}, overrides={"SUPERVISOR_PRICE_INPUT_PER_1M": "2", "SUPERVISOR_PRICE_CACHED_INPUT_PER_1M": "1", "SUPERVISOR_PRICE_OUTPUT_PER_1M": "8"})
    assert _sum_usage(calls, c.prices())["cost_estimate"] == 2.0


def test_model_switch_is_configuration_only(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_MODEL", "otro-modelo")
    monkeypatch.setenv("SUPERVISOR_PROVIDER", "openai")
    c = load_config()
    assert c.model == "otro-modelo" and c.sources["SUPERVISOR_MODEL"] == "env" and c.provider == "openai"
