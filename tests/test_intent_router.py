import json

import httpx

from credit.intent_router import DeepSeekIntentRouter


def test_router_is_disabled_without_explicit_mode_and_api_key(monkeypatch):
    monkeypatch.delenv("CREDIT_INTENT_ROUTER", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    router = DeepSeekIntentRouter.from_environment()

    assert router.enabled is False
    assert router.route("逾期率口径和血缘") is None


def test_router_sends_only_intent_classification_request_and_normalizes_order():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps({"intents": ["lineage", "definition"]})
        }}]})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    router = DeepSeekIntentRouter("test-secret", "deepseek-flash", client=client)

    assert router.route("逾期率口径和血缘") == ["definition", "lineage"]
    request = requests[0]
    payload = json.loads(request.content)
    assert request.url.path == "/chat/completions"
    assert request.headers["authorization"] == "Bearer test-secret"
    assert payload["response_format"] == {"type": "json_object"}
    assert "不生成 SQL" in payload["messages"][0]["content"]
    assert payload["messages"][1]["content"] == "逾期率口径和血缘"


def test_router_rejects_unknown_or_malformed_intents_for_deterministic_fallback():
    outputs = iter([
        {"intents": ["definition", "execute_sql"]},
        {"answer": "把表都查出来"},
    ])

    def respond(_request):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(next(outputs))
        }}]})

    router = DeepSeekIntentRouter("test-secret", "deepseek-flash",
                                  client=httpx.Client(transport=httpx.MockTransport(respond)))

    assert router.route("逾期率口径") is None
    assert router.route("逾期率口径") is None


def test_router_normalizes_empty_list_to_other_intent():
    def respond(_request):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": '{"intents": []}'
        }}]})

    router = DeepSeekIntentRouter("test-secret", "deepseek-flash",
                                  client=httpx.Client(transport=httpx.MockTransport(respond)))

    assert router.route("推荐一部电影") == ["other"]


def test_router_rejects_other_mixed_with_actionable_intent():
    def respond(_request):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": '{"intents":["other","query"]}'
        }}]})

    router = DeepSeekIntentRouter("test-secret", "deepseek-flash",
                                  client=httpx.Client(transport=httpx.MockTransport(respond)))

    assert router.route("逾期率") is None


def test_router_accepts_the_isolated_exploration_intent():
    def respond(_request):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": '{"intents":["exploration"]}'
        }}]})

    router = DeepSeekIntentRouter("test-secret", "deepseek-flash",
                                  client=httpx.Client(transport=httpx.MockTransport(respond)))

    assert router.route("探索试算 2026-03-01 申请拒绝率") == ["exploration"]
