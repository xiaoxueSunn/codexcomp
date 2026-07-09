"""Self-test for the fold state machine with canned upstream rounds.

Run: uv run python test_fold.py
"""
from __future__ import annotations

import asyncio
import json

from codexcomp.fold import DONE, RoundOpenError, fold, STEP


def reasoning_round(rid: str, reasoning_toks: int, text: str | None, enc: bool = True):
    """Canned upstream events for one round."""
    item = {"id": rid, "type": "reasoning", "summary": []}
    done_item = dict(item)
    if enc:
        done_item["encrypted_content"] = "ENC_" + rid
    evs = [
        {"type": "response.created", "sequence_number": 0,
         "response": {"id": "resp_1", "created_at": 111, "status": "in_progress"}},
        {"type": "response.in_progress", "sequence_number": 1, "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_item.done", "output_index": 0, "item": done_item},
    ]
    if text is not None:
        msg = {"id": "msg_" + rid, "type": "message", "role": "assistant"}
        evs += [
            {"type": "response.output_item.added", "output_index": 1, "item": msg},
            {"type": "response.output_text.delta", "output_index": 1,
             "item_id": msg["id"], "content_index": 0, "delta": text},
            {"type": "response.output_item.done", "output_index": 1,
             "item": {**msg, "content": [{"type": "output_text", "text": text}]}},
        ]
    evs.append({"type": "response.completed", "response": {
        "id": "resp_1", "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": reasoning_toks + (20 if text else 0),
                  "total_tokens": 120 + reasoning_toks,
                  "output_tokens_details": {"reasoning_tokens": reasoning_toks}},
    }})
    # NB: real upstream sends [DONE] after the terminal event; the fold stops at
    # the terminal, so DONE never reaches it — stream close is the terminator.
    return evs


async def test_happy_fold():
    opened_bodies = []
    rounds = [
        reasoning_round("rs_1", STEP - 2, "TRUNCATED ANSWER"),   # 516 -> continue
        reasoning_round("rs_2", 2 * STEP - 2, "STILL TRUNCATED"),  # 1034 -> continue
        reasoning_round("rs_3", 404, "REAL ANSWER"),             # clean
    ]

    async def opener(body):
        opened_bodies.append(body)
        idx = len(opened_bodies) - 1

        async def gen():
            for ev in rounds[idx]:
                yield ev
        return gen()

    out = []
    async for ev in fold({"model": "gpt-5.5", "input": [{"type": "message", "role": "user"}],
                          "stream": True}, opener):
        out.append(ev)

    # --- assertions -----------------------------------------------------------
    assert len(opened_bodies) == 3, f"expected 3 rounds, got {len(opened_bodies)}"

    # continuation bodies replay reasoning + commentary nudge, drop prev id
    b2 = opened_bodies[1]
    types2 = [i.get("type") for i in b2["input"]]
    assert types2 == ["message", "reasoning", "message"], types2
    assert b2["input"][-1]["phase"] == "commentary"
    assert "reasoning.encrypted_content" in b2["include"]
    b3 = opened_bodies[2]
    assert [i.get("type") for i in b3["input"]] == [
        "message", "reasoning", "message", "reasoning", "message"]

    dict_events = [e for e in out if isinstance(e, dict)]
    # exactly one terminal, and it is the LAST dict event
    terminals = [e for e in dict_events if e["type"].startswith("response.")
                 and e["type"] in ("response.completed", "response.failed", "response.incomplete")]
    assert len(terminals) == 1 and dict_events[-1] is terminals[0]
    term = terminals[0]["response"]

    # truncated messages are discarded; only the clean round's text is flushed
    deltas = [e["delta"] for e in dict_events if e["type"] == "response.output_text.delta"]
    assert deltas == ["REAL ANSWER"], deltas

    # sequence numbers proxy-owned and monotonic; output_index renumbered 0..3
    seqs = [e["sequence_number"] for e in dict_events]
    assert seqs == list(range(len(seqs))), "sequence not monotonic"
    ois = sorted({e.get("output_index") for e in dict_events if "output_index" in e})
    assert ois == [0, 1, 2, 3], ois  # 3 reasoning items + 1 flushed message

    # usage: reasoning summed, input from round 1, billed usage in metadata
    u = term["usage"]
    expect_reason = (STEP - 2) + (2 * STEP - 2) + 404
    assert u["output_tokens_details"]["reasoning_tokens"] == expect_reason
    assert u["input_tokens"] == 100
    assert term["metadata"]["proxy_billed_usage"]["input_tokens"] == 300
    assert [r["n"] for r in term["metadata"]["proxy_rounds"]] == [1, 2, None]

    # output preserved in order: rs_1, rs_2, rs_3 reasoning + final message
    otypes = [(i["type"], i.get("id")) for i in term["output"]]
    assert otypes == [("reasoning", "rs_1"), ("reasoning", "rs_2"),
                      ("reasoning", "rs_3"), ("message", "msg_rs_3")], otypes

    print("terminal usage:", json.dumps(u))


async def test_round1_rejected():
    """Upstream rejects round 1: fold itself yields the response.failed terminal."""
    async def opener(body):
        raise RoundOpenError(429, "quota exceeded")

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    assert len(out) == 1, out
    ev = out[0]
    assert ev["type"] == "response.failed", ev
    assert ev["response"]["status"] == "failed"
    assert ev["response"]["error"]["code"] == 429
    assert "quota exceeded" in ev["response"]["error"]["message"]
    assert ev["sequence_number"] == 0


async def test_continuation_open_fails():
    """Round 2 fails to open: incomplete terminal, round 1 reasoning kept,
    truncated message never flushed."""
    calls = []

    async def opener(body):
        calls.append(body)
        if len(calls) > 1:
            raise RoundOpenError(500, "boom")

        async def gen():
            for ev in reasoning_round("rs_1", STEP - 2, "TRUNCATED"):
                yield ev
        return gen()

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    assert len(calls) == 2, len(calls)
    term = out[-1]
    assert term["type"] == "response.incomplete", term
    resp = term["response"]
    assert resp["incomplete_details"]["reason"] == "upstream_error"
    assert resp["metadata"]["proxy_stopped_reason"] == "upstream_error"
    assert [i["type"] for i in resp["output"]] == ["reasoning"]
    deltas = [e["delta"] for e in out
              if isinstance(e, dict) and e["type"] == "response.output_text.delta"]
    assert deltas == [], deltas


async def test_upstream_eof():
    """Stream ends without a terminal event: incomplete terminal, tentative
    output is not an answer."""
    evs = reasoning_round("rs_1", 404, "TENTATIVE")[:-1]  # strip the terminal

    async def opener(body):
        async def gen():
            for ev in evs:
                yield ev
        return gen()

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    term = out[-1]
    assert term["type"] == "response.incomplete", term
    assert term["response"]["incomplete_details"]["reason"] == "upstream_eof"
    assert [i["type"] for i in term["response"]["output"]] == ["reasoning"]
    deltas = [e["delta"] for e in out
              if isinstance(e, dict) and e["type"] == "response.output_text.delta"]
    assert deltas == [], deltas


async def test_upstream_failed_preserves_error():
    """A terminal response.failed from upstream must stay transparent; losing
    response.error makes Codex reconnects impossible to diagnose from client
    sessions or proxy logs."""
    upstream = [
        {"type": "response.created", "sequence_number": 0,
         "response": {"id": "resp_1", "created_at": 111, "status": "in_progress"}},
        {"type": "response.failed", "response": {
            "id": "resp_1",
            "status": "failed",
            "error": {
                "type": "server_error",
                "code": "upstream_failed",
                "message": "backend failed after accepting request",
            },
        }},
    ]

    async def opener(body):
        async def gen():
            for ev in upstream:
                yield ev
        return gen()

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    term = out[-1]
    assert term["type"] == "response.failed", term
    resp = term["response"]
    assert resp["status"] == "failed"
    assert resp["error"]["type"] == "server_error"
    assert resp["error"]["code"] == "upstream_failed"
    assert "backend failed" in resp["error"]["message"]


async def test_upstream_failed_drops_tentative_buffered_output():
    """A failed terminal is not a clean answer. Buffered message/tool output from
    that round must not be streamed or persisted into terminal output."""
    upstream = [
        {"type": "response.created", "sequence_number": 0,
         "response": {"id": "resp_1", "created_at": 111, "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"id": "rs_1", "type": "reasoning", "summary": []}},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"id": "rs_1", "type": "reasoning",
                  "encrypted_content": "ENC_1", "summary": []}},
        {"type": "response.output_item.added", "output_index": 1,
         "item": {"id": "msg_1", "type": "message", "role": "assistant"}},
        {"type": "response.output_text.delta", "output_index": 1,
         "item_id": "msg_1", "content_index": 0, "delta": "tentative"},
        {"type": "response.output_item.done", "output_index": 1,
         "item": {"id": "msg_1", "type": "message", "role": "assistant",
                  "content": [{"type": "output_text", "text": "tentative"}]}},
        {"type": "response.failed", "response": {
            "id": "resp_1",
            "status": "failed",
            "usage": {"input_tokens": 50, "output_tokens": 61, "total_tokens": 111,
                      "output_tokens_details": {"reasoning_tokens": 40}},
            "error": {"type": "server_error", "message": "failed after output"},
        }},
    ]

    async def opener(body):
        async def gen():
            for ev in upstream:
                yield ev
        return gen()

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    deltas = [e["delta"] for e in out
              if isinstance(e, dict) and e["type"] == "response.output_text.delta"]
    assert deltas == [], deltas
    term = out[-1]
    assert term["type"] == "response.failed", term
    resp = term["response"]
    assert [(i["type"], i.get("id")) for i in resp["output"]] == [
        ("reasoning", "rs_1")
    ]
    assert resp["error"]["message"] == "failed after output"


async def test_upstream_incomplete_drops_tentative_buffered_output():
    """response.incomplete without a continuation fingerprint is not a clean
    answer, so buffered output stays tentative."""
    calls = []
    upstream = [
        {"type": "response.created", "sequence_number": 0,
         "response": {"id": "resp_1", "created_at": 111, "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"id": "rs_1", "type": "reasoning", "summary": []}},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"id": "rs_1", "type": "reasoning",
                  "encrypted_content": "ENC_1", "summary": []}},
        {"type": "response.output_item.added", "output_index": 1,
         "item": {"id": "msg_1", "type": "message", "role": "assistant"}},
        {"type": "response.output_item.done", "output_index": 1,
         "item": {"id": "msg_1", "type": "message", "role": "assistant",
                  "content": [{"type": "output_text", "text": "tentative"}]}},
        {"type": "response.incomplete", "response": {
            "id": "resp_1",
            "status": "incomplete",
            "usage": {"input_tokens": 50, "output_tokens": 80,
                      "total_tokens": 130,
                      "output_tokens_details": {"reasoning_tokens": 40}},
            "incomplete_details": {"reason": "max_output_tokens"},
        }},
    ]

    async def opener(body):
        calls.append(body)

        async def gen():
            for ev in upstream:
                yield ev
        return gen()

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    assert len(calls) == 1, "incomplete terminal must not trigger continuation"
    term = out[-1]
    assert term["type"] == "response.incomplete", term
    resp = term["response"]
    assert resp["incomplete_details"]["reason"] == "max_output_tokens"
    assert [(i["type"], i.get("id")) for i in resp["output"]] == [
        ("reasoning", "rs_1")
    ]


async def test_upstream_incomplete_can_continue_on_truncation_fingerprint():
    """An incomplete terminal can still be the 518n-2 truncation signal. Continue
    from encrypted reasoning, then only flush the final completed round output."""
    calls = []
    rounds = [
        [
            {"type": "response.created", "sequence_number": 0,
             "response": {"id": "resp_1", "created_at": 111, "status": "in_progress"}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"id": "rs_1", "type": "reasoning", "summary": []}},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"id": "rs_1", "type": "reasoning",
                      "encrypted_content": "ENC_1", "summary": []}},
            {"type": "response.output_item.added", "output_index": 1,
             "item": {"id": "msg_1", "type": "message", "role": "assistant"}},
            {"type": "response.output_item.done", "output_index": 1,
             "item": {"id": "msg_1", "type": "message", "role": "assistant",
                      "content": [{"type": "output_text", "text": "tentative"}]}},
            {"type": "response.incomplete", "response": {
                "id": "resp_1",
                "status": "incomplete",
                "usage": {"input_tokens": 50, "output_tokens": STEP - 2,
                          "total_tokens": 50 + STEP - 2,
                          "output_tokens_details": {"reasoning_tokens": STEP - 2}},
                "incomplete_details": {"reason": "max_output_tokens"},
            }},
        ],
        reasoning_round("rs_2", 40, "REAL ANSWER"),
    ]

    async def opener(body):
        calls.append(body)
        idx = len(calls) - 1

        async def gen():
            for ev in rounds[idx]:
                yield ev
        return gen()

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    assert len(calls) == 2, len(calls)
    assert [i.get("type") for i in calls[1]["input"]] == ["reasoning", "message"]
    deltas = [e["delta"] for e in out
              if isinstance(e, dict) and e["type"] == "response.output_text.delta"]
    assert deltas == ["REAL ANSWER"], deltas
    term = out[-1]
    assert term["type"] == "response.completed", term
    assert [(i["type"], i.get("id")) for i in term["response"]["output"]] == [
        ("reasoning", "rs_1"),
        ("reasoning", "rs_2"),
        ("message", "msg_rs_2"),
    ]


async def test_interleaved_web_search_ordering():
    """Terminal output preserves upstream arrival order: each buffered item
    (message, web_search_call, function_call, ...) stays right after its owning
    reasoning item. If reasoning is hoisted before its dependents, ModelHub
    rejects the replay with 400 'was provided without its required reasoning
    item'."""
    # upstream sends: rs_A, ws_A(ref rs_A), rs_B, msg_B(ref rs_B)
    upstream = [
        {"type": "response.created", "sequence_number": 0,
         "response": {"id": "resp_1", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"id": "rs_A", "type": "reasoning", "summary": []}},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"id": "rs_A", "type": "reasoning",
                  "encrypted_content": "ENC_A", "summary": []}},
        {"type": "response.output_item.added", "output_index": 1,
         "item": {"id": "ws_A", "type": "web_search_call"}},
        {"type": "response.output_item.done", "output_index": 1,
         "item": {"id": "ws_A", "type": "web_search_call",
                  "action": {"type": "search", "query": "hello"}}},
        {"type": "response.output_item.added", "output_index": 2,
         "item": {"id": "rs_B", "type": "reasoning", "summary": []}},
        {"type": "response.output_item.done", "output_index": 2,
         "item": {"id": "rs_B", "type": "reasoning",
                  "encrypted_content": "ENC_B", "summary": []}},
        {"type": "response.output_item.added", "output_index": 3,
         "item": {"id": "msg_B", "type": "message", "role": "assistant"}},
        {"type": "response.output_item.done", "output_index": 3,
         "item": {"id": "msg_B", "type": "message", "role": "assistant",
                  "content": [{"type": "output_text", "text": "done"}]}},
        {"type": "response.completed", "response": {
            "id": "resp_1", "status": "completed",
            "usage": {"input_tokens": 50, "output_tokens": 60,
                      "total_tokens": 110,
                      "output_tokens_details": {"reasoning_tokens": 40}},
        }},
    ]

    async def opener(body):
        async def gen():
            for ev in upstream:
                yield ev
        return gen()

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    term = out[-1]
    assert term["type"] == "response.completed", term
    order = [(i["type"], i.get("id")) for i in term["response"]["output"]]
    assert order == [
        ("reasoning", "rs_A"),
        ("web_search_call", "ws_A"),
        ("reasoning", "rs_B"),
        ("message", "msg_B"),
    ], order


async def _assert_stream_ordering_when_buffered_item_finishes_late(
    buffered_item: dict,
    buffered_done_extra: dict | None = None,
):
    """The live stream must not let a later reasoning item overtake an earlier
    buffered item. Codex Desktop persists streamed response_item order as replay
    input; if it sees rs_A, rs_B, msg_A, ModelHub rejects msg_A as detached from
    rs_A on the next turn."""
    buffered_id = buffered_item["id"]
    buffered_done = {**buffered_item, **(buffered_done_extra or {})}
    upstream = [
        {"type": "response.created", "sequence_number": 0,
         "response": {"id": "resp_1", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"id": "rs_A", "type": "reasoning", "summary": []}},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"id": "rs_A", "type": "reasoning",
                  "encrypted_content": "ENC_A", "summary": []}},
        {"type": "response.output_item.added", "output_index": 1,
         "item": buffered_item},
        {"type": "response.output_item.added", "output_index": 2,
         "item": {"id": "rs_B", "type": "reasoning", "summary": []}},
        {"type": "response.output_item.done", "output_index": 2,
         "item": {"id": "rs_B", "type": "reasoning",
                  "encrypted_content": "ENC_B", "summary": []}},
        {"type": "response.output_text.delta", "output_index": 1,
         "item_id": buffered_id, "content_index": 0, "delta": "working"},
        {"type": "response.output_item.done", "output_index": 1,
         "item": buffered_done},
        {"type": "response.completed", "response": {
            "id": "resp_1", "status": "completed",
            "usage": {"input_tokens": 50, "output_tokens": 60,
                      "total_tokens": 110,
                      "output_tokens_details": {"reasoning_tokens": 40}},
        }},
    ]

    async def opener(body):
        async def gen():
            for ev in upstream:
                yield ev
        return gen()

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    streamed_items = [
        (ev["item"]["type"], ev["item"].get("id"))
        for ev in out
        if isinstance(ev, dict) and ev["type"] == "response.output_item.done"
    ]
    assert streamed_items == [
        ("reasoning", "rs_A"),
        (buffered_item["type"], buffered_id),
        ("reasoning", "rs_B"),
    ], streamed_items

    term = out[-1]
    terminal_items = [(i["type"], i.get("id")) for i in term["response"]["output"]]
    assert terminal_items == streamed_items, terminal_items


async def test_stream_ordering_when_message_finishes_late():
    await _assert_stream_ordering_when_buffered_item_finishes_late(
        {"id": "msg_A", "type": "message", "role": "assistant"},
        {"content": [{"type": "output_text", "text": "working"}]},
    )


async def test_stream_ordering_when_web_search_finishes_late():
    await _assert_stream_ordering_when_buffered_item_finishes_late(
        {"id": "ws_A", "type": "web_search_call"},
        {"action": {"type": "search", "query": "hello"}},
    )


async def test_stream_ordering_when_function_call_finishes_late():
    await _assert_stream_ordering_when_buffered_item_finishes_late(
        {"id": "fc_A", "type": "function_call", "name": "exec_command",
         "call_id": "call_A", "arguments": "{}"},
    )


async def main():
    await test_happy_fold()
    await test_round1_rejected()
    await test_continuation_open_fails()
    await test_upstream_eof()
    await test_upstream_failed_preserves_error()
    await test_upstream_failed_drops_tentative_buffered_output()
    await test_upstream_incomplete_drops_tentative_buffered_output()
    await test_upstream_incomplete_can_continue_on_truncation_fingerprint()
    await test_interleaved_web_search_ordering()
    await test_stream_ordering_when_message_finishes_late()
    await test_stream_ordering_when_web_search_finishes_late()
    await test_stream_ordering_when_function_call_finishes_late()
    print("fold self-test: ALL PASS")


asyncio.run(main())
