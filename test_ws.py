"""Self-test for the WebSocket transport's stateful-protocol handling.

Replays the frame sequence Codex 0.142.x actually sends over
responses_websockets (prewarm `generate:false`, then `previous_response_id` +
incremental input) against a mock SSE upstream, and asserts the upstream only
ever sees stateless full-input bodies.

Run: uv run python test_ws.py
"""
import json

import httpx
from starlette.testclient import TestClient

from codexcomp.server import build_app

USER1 = {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "hi"}]}
FCO = {"type": "function_call_output", "call_id": "call_1", "output": "ok"}

upstream_calls: list[dict] = []
upstream_requests: list[httpx.Request] = []


def canned_sse(rid: str) -> bytes:
    msg = {"id": f"msg_{rid}", "type": "message", "role": "assistant",
           "content": [{"type": "output_text", "text": "ANSWER " + rid}]}
    events = [
        {"type": "response.created", "response": {"id": rid, "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": msg},
        {"type": "response.output_item.done", "output_index": 0, "item": msg},
        {"type": "response.completed", "response": {
            "id": rid, "status": "completed",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                      "output_tokens_details": {"reasoning_tokens": 3}}}},
    ]
    out = b"".join(f"data: {json.dumps(ev)}\n\n".encode() for ev in events)
    return out + b"data: [DONE]\n\n"


def mock_upstream(request: httpx.Request) -> httpx.Response:
    upstream_requests.append(request)
    body = json.loads(request.content)
    upstream_calls.append(body)
    if body.get("model") == "fail-me":
        return httpx.Response(400, json={"detail": "boom"})
    rid = f"resp_up_{len(upstream_calls)}"
    return httpx.Response(200, content=canned_sse(rid),
                          headers={"content-type": "text/event-stream"})


def make_client() -> TestClient:
    app = build_app("http://upstream.test/v1")
    app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_upstream))
    return TestClient(app)


def make_modelhub_client() -> TestClient:
    app = build_app("http://upstream.test/v1", strip_authorization=True)
    app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_upstream))
    return TestClient(app)


def recv_until_terminal(ws) -> list[dict]:
    frames = []
    while True:
        frames.append(json.loads(ws.receive_text()))
        if frames[-1]["type"] in ("response.completed", "response.failed",
                                  "response.incomplete"):
            return frames


def test_prewarm_then_incremental():
    """Prewarm is acked locally; empty-delta and tool-loop frames are rebuilt
    to full input; upstream never sees ws-only fields."""
    client = make_client()
    with client.websocket_connect("/v1/responses") as ws:
        # 1. prewarm: generate=false, full input — must NOT reach upstream
        ws.send_text(json.dumps({"type": "response.create", "model": "gpt-5.5",
                                 "stream": True, "input": [USER1],
                                 "generate": False}))
        ack = json.loads(ws.receive_text())
        assert ack["type"] == "response.completed", ack
        prewarm_id = ack["response"]["id"]
        assert prewarm_id.startswith("resp_codexcomp_prewarm_"), prewarm_id
        assert ack["response"]["usage"]["total_tokens"] == 0
        assert upstream_calls == [], "prewarm must not reach upstream"

        # 2. real request: previous_response_id = prewarm id, EMPTY delta
        ws.send_text(json.dumps({"type": "response.create", "model": "gpt-5.5",
                                 "stream": True, "input": [],
                                 "previous_response_id": prewarm_id}))
        frames = recv_until_terminal(ws)
        term = frames[-1]
        assert term["type"] == "response.completed"
        up1 = upstream_calls[0]
        assert up1["input"] == [USER1], up1["input"]
        assert "previous_response_id" not in up1 and "generate" not in up1
        seqs = [f["sequence_number"] for f in frames]
        assert seqs == list(range(len(seqs))), seqs
        answer_item = term["response"]["output"][-1]

        # 3. tool loop: previous_response_id = upstream response id, delta=[FCO]
        ws.send_text(json.dumps({"type": "response.create", "model": "gpt-5.5",
                                 "stream": True, "input": [FCO],
                                 "previous_response_id": term["response"]["id"]}))
        frames = recv_until_terminal(ws)
        assert frames[-1]["type"] == "response.completed"
        up2 = upstream_calls[1]
        assert up2["input"] == [USER1, answer_item, FCO], \
            [i.get("type") for i in up2["input"]]
        assert "previous_response_id" not in up2


def test_unknown_previous_response():
    """An id this session never issued fails loud and drops the connection."""
    client = make_client()
    with client.websocket_connect("/v1/responses") as ws:
        ws.send_text(json.dumps({"type": "response.create", "model": "gpt-5.5",
                                 "stream": True, "input": [],
                                 "previous_response_id": "resp_bogus"}))
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "response.failed", frame
        assert frame["response"]["error"]["code"] == "unknown_previous_response_id"


def test_failed_turn_invalidates_state():
    """After a failed request, its would-be id must not be reusable."""
    client = make_client()
    with client.websocket_connect("/v1/responses") as ws:
        ws.send_text(json.dumps({"type": "response.create", "model": "gpt-5.5",
                                 "stream": True, "input": [USER1],
                                 "generate": False}))
        prewarm_id = json.loads(ws.receive_text())["response"]["id"]
        ws.send_text(json.dumps({"type": "response.create", "model": "fail-me",
                                 "stream": True, "input": [],
                                 "previous_response_id": prewarm_id}))
        frames = recv_until_terminal(ws)
        assert frames[-1]["type"] == "response.failed"
        # the prewarm id was consumed by note_request; reusing it must fail loud
        ws.send_text(json.dumps({"type": "response.create", "model": "gpt-5.5",
                                 "stream": True, "input": [],
                                 "previous_response_id": prewarm_id}))
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "response.failed"
        assert frame["response"]["error"]["code"] == "unknown_previous_response_id"


def test_post_sse_unchanged():
    """The HTTP fallback path still passes full-input bodies straight through."""
    client = make_client()
    n_before = len(upstream_calls)
    resp = client.post("/v1/responses",
                       json={"model": "gpt-5.5", "stream": True, "input": [USER1]})
    assert resp.status_code == 200
    assert "response.completed" in resp.text
    assert upstream_calls[n_before]["input"] == [USER1]


def test_modelhub_query_and_auth_handling():
    """ModelHub-style providers authenticate via query params; do not forward
    Codex's OpenAI bearer token as an upstream Authorization header."""
    client = make_modelhub_client()
    resp = client.post(
        "/v1/responses?ak=AK123&api-version=2025-04-01-preview",
        json={"model": "gpt-5.5", "stream": True, "input": [USER1]},
        headers={"authorization": "Bearer should-not-forward"},
    )
    assert resp.status_code == 200
    request = upstream_requests[-1]
    assert str(request.url) == (
        "http://upstream.test/v1/responses?ak=AK123&api-version=2025-04-01-preview"
    )
    assert "authorization" not in request.headers


def main():
    test_prewarm_then_incremental()
    upstream_calls.clear()
    test_unknown_previous_response()
    upstream_calls.clear()
    test_failed_turn_invalidates_state()
    upstream_calls.clear()
    upstream_requests.clear()
    test_post_sse_unchanged()
    upstream_calls.clear()
    upstream_requests.clear()
    test_modelhub_query_and_auth_handling()
    print("ws transport self-test: ALL PASS")


main()
