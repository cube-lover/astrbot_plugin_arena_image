"""Synthetic OFFLINE candidate contracts, not captured/live acceptance fixtures.

All URLs use .invalid and all entity/run IDs are synthetic UUIDs. No transport,
browser, server, HTTP request, polling or broad baseline updates are involved.
The latest user report says the native terminal generation succeeded without
metadata; no live data/model IDs are persisted as fixtures here.
"""

from __future__ import annotations

import copy
import json
import socket
import traceback

import pytest
from astrbot_plugin_arena_image import arena_battle, arena_video, bridge_client

EVALUATION_ID = "11111111-1111-7111-8111-111111111111"
MESSAGE_A_ID = "22222222-2222-7222-8222-222222222222"
MESSAGE_B_ID = "33333333-3333-7333-8333-333333333333"
USER_ID = "44444444-4444-7444-8444-444444444444"
OTHER_ID = "55555555-5555-7555-8555-555555555555"
MODEL_A_ID = "66666666-6666-7666-8666-666666666666"
MODEL_B_ID = "77777777-7777-7777-8777-777777777777"
RUN_A_ID = "88888888-8888-7888-8888-888888888888"
RUN_B_ID = "99999999-9999-7999-8999-999999999999"
URL_A = "https://videos.example.invalid/a.mp4?X-Amz-Signature=offline-a-secret"
URL_B = "https://videos.example.invalid/b.mp4?X-Amz-Signature=offline-b-secret"
THUMB_A = "https://thumbnails.example.invalid/a.jpg"
THUMB_B = "https://thumbnails.example.invalid/b.jpg"
HIDDEN_NAME = "Hidden Offline Provider Model"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline video protocol must not use network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def context():
    return {
        "evaluationSessionId": EVALUATION_ID,
        "messageAId": MESSAGE_A_ID,
        "messageBId": MESSAGE_B_ID,
        "browser_scope": "cdp:offline-video-browser",
        "userMessageId": USER_ID,
        "modality": "video",
        "mode": "battle",
    }


def record(side="a", *, session=False, **extra):
    value = {
        "url": URL_A if side == "a" else URL_B,
        "thumbnailUrl": THUMB_A if side == "a" else THUMB_B,
    }
    if session:
        value["contentType"] = "video/mp4"
    else:
        value.update(type="video", mimeType="video/mp4")
    return {**value, **extra}


def row(prefix, value):
    return prefix + ":" + json.dumps(value)


def session(status_a="pending", status_b="pending"):
    return {
        "id": EVALUATION_ID,
        "mode": "battle",
        "modality": "video",
        "modelAId": MODEL_A_ID,
        "modelBId": MODEL_B_ID,
        "revealedModels": [{"id": MODEL_A_ID, "displayName": HIDDEN_NAME}],
        "messages": [
            {
                "id": USER_ID,
                "evaluationSessionId": EVALUATION_ID,
                "role": "user",
                "status": "success",
                "content": "Synthetic video prompt",
                "experimental_attachments": [],
                "metadata": {},
            },
            *[
                {
                    "id": MESSAGE_A_ID if side == "a" else MESSAGE_B_ID,
                    "evaluationSessionId": EVALUATION_ID,
                    "role": "assistant",
                    "participantPosition": side,
                    "status": status,
                    "modelId": MODEL_A_ID if side == "a" else MODEL_B_ID,
                    "content": HIDDEN_NAME,
                    "metadata": {
                        "videoRun": {
                            "runId": RUN_A_ID if side == "a" else RUN_B_ID,
                            "modelId": MODEL_A_ID,
                            "provider": HIDDEN_NAME,
                        }
                    },
                    "experimental_attachments": [record(side, session=True)],
                    "error": {
                        "message": HIDDEN_NAME,
                        "modelId": MODEL_B_ID,
                        "signedUrl": URL_B,
                    },
                }
                for side, status in (("a", status_a), ("b", status_b))
            ],
        ],
    }


def public_error(exc):
    return (
        str(exc)
        + repr(exc)
        + json.dumps(exc.payload)
        + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    )


def assert_anonymous(value, *, no_urls=False):
    rendered = value if isinstance(value, str) else json.dumps(value)
    secrets = [MODEL_A_ID, MODEL_B_ID, HIDDEN_NAME]
    if no_urls:
        secrets += [URL_A, URL_B, "offline-a-secret", "offline-b-secret"]
    for secret in secrets:
        assert secret not in rendered


def test_context_extends_base_allowlist_without_mutation_or_model_fields():
    source = context()
    source.update(modelAId=MODEL_A_ID, modelName=HIDDEN_NAME, cx={"ignored": True})
    source["messageAId"] = "ABCDEFAB-CDEF-7ABC-8ABC-ABCDEFABCDEF"
    original = copy.deepcopy(source)
    result = arena_video.video_context(source)
    assert result == {
        **arena_battle.battle_context(source),
        "mode": "battle",
        "modality": "video",
        "userMessageId": USER_ID,
    }
    assert result["messageAId"] == source["messageAId"].lower()
    assert source == original
    assert_anonymous(result)
    # Old Battle contexts remain usable, while video invariants are added.
    source.pop("userMessageId")
    source.pop("modality")
    source.pop("mode")
    assert "userMessageId" not in arena_video.video_context(source)
    assert arena_video.video_context(source)["modality"] == "video"


@pytest.mark.parametrize(
    "field,value",
    [
        ("evaluationSessionId", HIDDEN_NAME),
        ("messageAId", None),
        ("messageBId", {}),
        ("userMessageId", "not-a-uuid"),
        ("messageBId", MESSAGE_A_ID),
        ("messageAId", EVALUATION_ID),
        ("userMessageId", MESSAGE_B_ID),
        ("browser_scope", ""),
        ("browser_scope", 12),
        ("browser_scope", "bad\nidentifier"),
        ("browser_scope", "x" * 513),
        ("mode", "direct"),
        ("modality", "image"),
    ],
)
def test_context_rejects_invalid_identity_scope_or_modality(field, value):
    source = context()
    source[field] = value
    with pytest.raises(bridge_client.BridgeError) as caught:
        arena_video.video_context(source)
    assert_anonymous(public_error(caught.value), no_urls=True)


@pytest.mark.parametrize("value", [None, [], "not-json", {}])
def test_context_requires_fields_and_dictionary(value):
    with pytest.raises(bridge_client.BridgeError):
        arena_video.video_context(value)


def test_stream_interleaved_lanes_records_deduplication_and_independent_finish():
    stream = "\n".join(
        [
            "event: message",
            ' data: a0:" A first " ',
            'data: b0:"B first "',
            'a0:"second A"',
            'b0:"second B"',
            row("b2", [record("b")]),
            "data: " + row("a2", record()),
            row("a2", [record()]),
            row("ad", {"finishReason": "stop"}),
            row("bd", {"finishReason": "length"}),
            row("c2", [record()]),
            row("ag", HIDDEN_NAME),
            row("bc", [record()]),
            "data: [DONE]",
        ]
    )
    result = arena_video.parse_video_stream(stream)
    assert result == {
        "a": {
            "text": "A first second A",
            "videos": [record_without_type()],
            "error": "",
            "finish_reason": "stop",
        },
        "b": {
            "text": "B first second B",
            "videos": [record_without_type("b")],
            "error": "",
            "finish_reason": "length",
        },
    }
    assert "completed" not in result["a"]
    assert_anonymous(result)


def record_without_type(side="a"):
    value = record(side)
    value.pop("type")
    return value


@pytest.mark.parametrize("body", ["", None, "event: done\n: heartbeat\ndata: [DONE]"])
def test_empty_or_unrelated_stream_is_not_completion(body):
    empty = {"text": "", "videos": [], "error": "", "finish_reason": ""}
    assert arena_video.parse_video_stream(body) == {"a": empty, "b": empty}


def test_url_without_finish_and_finish_without_url_are_separate():
    result = arena_video.parse_video_stream(
        row("a2", [record()]) + "\n" + row("bd", {"finishReason": "stop"})
    )
    assert result["a"]["videos"]
    assert result["a"]["finish_reason"] == ""
    assert result["b"]["videos"] == []
    assert result["b"]["finish_reason"] == "stop"
    assert all("completed" not in lane for lane in result.values())


@pytest.mark.parametrize(
    "bad",
    [
        "a2:{broken",
        "a2:",
        "a0:123",
        "a0:null",
        'a0:{"error":"hidden"}',
        'ad:{"finishReason":[]}',
        'ad:{"finishReason":"Hidden Offline Provider Model"}',
        'ad:{"url":"https://videos.example.invalid/not-finish.mp4"}',
        "a2:[null]",
        "a2:[123]",
        row("a2", [{"type": "video", "url": URL_A}]),
        row("a2", [record(mimeType="video/webm")]),
    ],
)
def test_malformed_recognized_rows_fail_only_their_lane_and_remain_sticky(bad):
    result = arena_video.parse_video_stream(
        "\n".join(
            [
                row("a2", [record()]),
                bad,
                row("a2", [record()]),
                row("b2", [record("b")]),
            ]
        )
    )
    assert result["a"]["error"]
    assert result["a"]["videos"] == []
    assert result["b"]["videos"] == [record_without_type("b")]
    assert_anonymous(result["a"], no_urls=True)


@pytest.mark.parametrize(
    "value",
    [
        HIDDEN_NAME + " " + URL_A,
        {"provider": HIDDEN_NAME, "modelId": MODEL_A_ID, "signedUrl": URL_A},
        [MODEL_B_ID, URL_B],
        None,
    ],
)
@pytest.mark.parametrize("side", ["a", "b"])
def test_stream_errors_are_anonymous_and_do_not_erase_other_lane(value, side):
    other = "b" if side == "a" else "a"
    result = arena_video.parse_video_stream(
        "\n".join(
            [
                row(side + "0", "discarded partial text"),
                row(side + "2", [record(side)]),
                row(side + "3", value),
                row(side + "d", {"finishReason": "stop"}),
                row(other + "2", [record(other)]),
            ]
        )
    )
    assert result[side]["error"]
    assert result[side]["text"] == ""
    assert result[side]["videos"] == []
    assert result[other]["error"] == ""
    assert result[other]["videos"]
    assert_anonymous(result[side], no_urls=True)


def test_malformed_error_and_error_finish_do_not_expose_payloads():
    for stream in (
        'a3:{broken "' + HIDDEN_NAME + '"',
        row("ad", {"finishReason": "error", "error": HIDDEN_NAME, "url": URL_A}),
        row("ad", {"finishReason": "content-filter", "modelId": MODEL_A_ID}),
    ):
        result = arena_video.parse_video_stream(row("a2", [record()]) + "\n" + stream)
        assert result["a"]["error"]
        assert result["a"]["videos"] == []
        assert_anonymous(result["a"], no_urls=True)


def test_only_explicit_top_level_video_records_are_collected():
    result = arena_video.parse_video_stream(
        row(
            "a2",
            [
                {"type": "image", "url": URL_A},
                {"type": "metadata", "modelId": MODEL_A_ID, "nested": record()},
                {"url": URL_A, "mimeType": "video/mp4"},
                record(),
            ],
        )
    )
    assert result["a"]["videos"] == [record_without_type()]
    assert_anonymous(result)


UNSAFE_URLS = [
    "http://videos.example.invalid/a.mp4",
    "//videos.example.invalid/a.mp4",
    "file:///D:/offline.mp4",
    "data:video/mp4;base64,offline",
    "javascript:offline",
    "https://user:pass@videos.example.invalid/a.mp4",
    "https://user@videos.example.invalid/a.mp4",
    "https://@videos.example.invalid/a.mp4",
    "https:///a.mp4",
    "https://videos.example.invalid:bad/a.mp4",
    "https://videos.example.invalid:99999/a.mp4",
    "https://videos.example.invalid:0/a.mp4",
    "https://videos.example.invalid:/a.mp4",
    "https://videos.example.invalid/a.mp4#private",
    "https://videos.example.invalid/a\n.mp4",
    " https://videos.example.invalid/a.mp4",
    "https://videos.example.invalid\\@other.example.invalid/a.mp4",
    "https://bad_host.example.invalid/a.mp4",
    "https://.example.invalid/a.mp4",
    "https://videos.example.invalid/a%0d%0a.mp4",
    "https://videos.example.invalid/a%zz.mp4",
    "https://[bad.example.invalid]/a.mp4",
    pytest.param("https://videos.example.invalid/" + "x" * 16384, id="overlong-url"),
    None,
    42,
    {},
    [],
]


@pytest.mark.parametrize("url", UNSAFE_URLS)
@pytest.mark.parametrize("field", ["url", "thumbnailUrl"])
def test_unsafe_stream_media_urls_fail_closed(url, field):
    result = arena_video.parse_video_stream(row("a2", [record(**{field: url})]))
    # An absent thumbnail is allowed; an absent video is not.
    if field == "thumbnailUrl" and url is None:
        assert result["a"]["error"] == ""
        assert result["a"]["videos"][0]["thumbnailUrl"] == ""
    else:
        assert result["a"]["error"]
        assert result["a"]["videos"] == []


def test_optional_thumbnail_signed_https_and_nonstandard_port_are_preserved():
    video = record(url="https://videos.example.invalid:8443/output?signature=offline")
    video.pop("thumbnailUrl")
    result = arena_video.parse_video_stream(row("a2", video))
    assert result["a"]["videos"] == [
        {
            "url": video["url"],
            "mimeType": "video/mp4",
            "thumbnailUrl": "",
        }
    ]


def test_duplicate_url_conflicting_metadata_and_finish_are_not_silently_accepted():
    for stream in (
        row("a2", [record(), record(thumbnailUrl=THUMB_B)]),
        row("ad", {"finishReason": "stop"}) + "\n" + row("ad", {"finishReason": "length"}),
    ):
        assert arena_video.parse_video_stream(stream)["a"]["error"]


@pytest.mark.parametrize("status", ["pending", "resampling", "failed", "flagged", "stopped"])
def test_non_success_session_status_ignores_even_present_stale_attachments(status):
    source = session(status, status)
    original = copy.deepcopy(source)
    result = arena_video.validate_video_session(source, context())
    assert result["completed"] is False
    for side, run_id in (("a", RUN_A_ID), ("b", RUN_B_ID)):
        lane = result["sides"][side]
        assert lane["status"] == status
        assert lane["run_id"] == run_id
        assert lane["videos"] == []
        assert lane["completed"] is False
        assert lane["finish_reason"] == ""
        assert bool(lane["error"]) == (status in {"failed", "flagged", "stopped"})
    assert source == original
    assert_anonymous(result, no_urls=True)
    # Non-success attachment content must not be interpreted, even if malformed.
    source["messages"][1]["experimental_attachments"] = {"url": "file:///D:/offline"}
    assert arena_video.validate_video_session(source, context())["sides"]["a"]["videos"] == []


def test_explicit_success_attachments_complete_both_sides_without_revealing_models():
    source = session("success", "success")
    result = arena_video.validate_video_session(source, context())
    assert result["completed"] is True
    assert result["evaluation_id"] == EVALUATION_ID
    assert set(result) == {"evaluation_id", "sides", "context", "completed"}
    for side in ("a", "b"):
        lane = result["sides"][side]
        assert lane["completed"] is True
        assert lane["status"] == "success"
        assert lane["videos"] == [record_without_type(side)]
        assert lane["text"] == ""
        assert lane["finish_reason"] == ""
        assert lane["error"] == ""
    assert_anonymous(result)
    result["sides"]["a"]["videos"][0]["url"] = "changed-result"
    assert source["messages"][1]["experimental_attachments"][0]["url"] == URL_A


@pytest.mark.parametrize("other_status", ["pending", "resampling", "failed", "flagged", "stopped"])
def test_partial_session_never_marks_battle_complete(other_status):
    result = arena_video.validate_video_session(session("success", other_status), context())
    assert result["sides"]["a"]["completed"] is True
    assert result["sides"]["b"]["completed"] is False
    assert result["sides"]["b"]["videos"] == []
    assert result["completed"] is False


@pytest.mark.parametrize("attachments", [[], [{"url": THUMB_A, "contentType": "image/jpeg"}]])
def test_success_without_video_stays_unconfirmed(attachments):
    source = session("success", "success")
    source["messages"][1]["experimental_attachments"] = attachments
    result = arena_video.validate_video_session(source, context())
    assert result["sides"]["a"]["status"] == "success"
    assert result["sides"]["a"]["completed"] is False
    assert result["sides"]["a"]["videos"] == []
    assert result["sides"]["a"]["error"]
    assert result["completed"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", OTHER_ID),
        ("id", None),
        ("mode", "direct"),
        ("mode", None),
        ("modality", "image"),
        ("modality", None),
        ("messages", None),
        ("messages", {}),
        ("messages", []),
        ("messages", [None]),
    ],
)
def test_session_requires_exact_evaluation_video_battle_and_messages(field, value):
    source = session()
    source[field] = value
    with pytest.raises(bridge_client.BridgeError) as caught:
        arena_video.validate_video_session(source, context())
    assert_anonymous(public_error(caught.value), no_urls=True)


@pytest.mark.parametrize("side_index", [1, 2])
@pytest.mark.parametrize(
    "field,value",
    [
        ("id", OTHER_ID),
        ("evaluationSessionId", OTHER_ID),
        ("evaluationSessionId", None),
        ("role", "user"),
        ("participantPosition", "c"),
        ("participantPosition", None),
        ("status", None),
        ("status", "completed"),
        ("status", True),
        ("status", {}),
    ],
)
def test_cross_session_side_and_invalid_status_are_rejected(side_index, field, value):
    source = session("success", "success")
    source["messages"][side_index][field] = value
    with pytest.raises(bridge_client.BridgeError):
        arena_video.validate_video_session(source, context())


def test_swapped_side_ids_duplicate_assistants_missing_side_and_wrong_context_rejected():
    variants = []
    source = session("success", "success")
    source["messages"][1]["id"], source["messages"][2]["id"] = MESSAGE_B_ID, MESSAGE_A_ID
    variants.append(source)
    source = session()
    source["messages"].append(copy.deepcopy(source["messages"][1]))
    variants.append(source)
    source = session()
    extra = copy.deepcopy(source["messages"][1])
    extra["id"] = OTHER_ID
    source["messages"].append(extra)
    variants.append(source)
    source = session()
    source["messages"].pop()
    variants.append(source)
    for source in variants:
        with pytest.raises(bridge_client.BridgeError):
            arena_video.validate_video_session(source, context())
    wrong_context = context()
    wrong_context["evaluationSessionId"] = OTHER_ID
    with pytest.raises(bridge_client.BridgeError):
        arena_video.validate_video_session(session(), wrong_context)


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "HTTP 200",
        {"status": 200, "data": session()},
        {"evaluation": session()},
        {"initialState": session()},
        json.dumps(session()),
    ],
)
def test_session_does_not_search_wrappers_or_infer_http_success(data):
    with pytest.raises(bridge_client.BridgeError):
        arena_video.validate_video_session(data, context())


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_success_session_unsafe_urls_raise_sanitized_errors(url):
    source = session("success", "success")
    source["messages"][1]["experimental_attachments"][0]["url"] = url
    with pytest.raises(bridge_client.BridgeError) as caught:
        arena_video.validate_video_session(source, context())
    assert_anonymous(public_error(caught.value), no_urls=True)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "attachments",
    [
        None,
        {},
        [None],
        [record(session=True, contentType="video/webm")],
        [record(session=True, thumbnailUrl="http://thumbnails.example.invalid/a.jpg")],
        [{"url": URL_A}],
        [record(session=True), record(session=True, thumbnailUrl=THUMB_B)],
    ],
)
def test_success_session_malformed_attachments_rejected(attachments):
    source = session("success", "success")
    source["messages"][1]["experimental_attachments"] = attachments
    with pytest.raises(bridge_client.BridgeError):
        arena_video.validate_video_session(source, context())


@pytest.mark.parametrize("metadata", [None, {}, {"videoRun": None}])
def test_missing_workflow_metadata_is_not_completion_evidence(metadata):
    source = session()
    source["messages"][1]["metadata"] = metadata
    result = arena_video.validate_video_session(source, context())
    assert result["sides"]["a"]["run_id"] == ""
    assert result["completed"] is False


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        {"videoRun": []},
        {"videoRun": {}},
        {"videoRun": {"runId": None}},
        {"videoRun": {"runId": 123}},
        {"videoRun": {"runId": URL_A}},
        {"videoRun": {"runId": "x" * 129}},
        {"videoRun": {"runId": HIDDEN_NAME}},
    ],
)
def test_invalid_workflow_metadata_is_rejected_without_echo(metadata):
    source = session()
    source["messages"][1]["metadata"] = metadata
    with pytest.raises(bridge_client.BridgeError) as caught:
        arena_video.validate_video_session(source, context())
    assert_anonymous(public_error(caught.value), no_urls=True)


@pytest.mark.parametrize(
    "body",
    [
        "x" * (arena_video.MAX_INPUT_BYTES + 1),
        "\u4e00" * (arena_video.MAX_INPUT_BYTES // 3 + 1),
        "\n" * (arena_video.MAX_LINES + 1),
        "x" * (arena_video.MAX_LINE_BYTES + 1),
    ],
    ids=["input-bytes", "utf8-bytes", "line-count", "line-bytes"],
)
def test_global_stream_limits_raise_sanitized_errors(body):
    with pytest.raises(bridge_client.BridgeError) as caught:
        arena_video.parse_video_stream(body)
    assert caught.value.code == "video_protocol_limit"
    assert_anonymous(public_error(caught.value), no_urls=True)


def test_stream_depth_text_and_video_count_are_bounded():
    deeply_nested = "[" * 2000 + "0" + "]" * 2000
    assert arena_video.parse_video_stream("a2:" + deeply_nested)["a"]["error"]
    videos = [
        record(url=f"https://videos.example.invalid/{n}.mp4")
        for n in range(arena_video.MAX_VIDEOS_PER_SIDE + 1)
    ]
    assert arena_video.parse_video_stream(row("a2", videos))["a"]["error"]
    text = "x" * 40000
    result = arena_video.parse_video_stream(row("a0", text) + "\n" + row("a0", text))
    assert result["a"]["error"]
    assert result["a"]["text"] == ""


def test_session_tree_size_depth_cycles_node_count_and_message_count_are_bounded():
    variants = []
    source = session()
    source["extra"] = "x" * (arena_video.MAX_INPUT_BYTES + 1)
    variants.append(source)
    source = session()
    source["extra"] = source
    variants.append(source)
    source = session()
    source["extra"] = [None] * arena_video.MAX_NODES
    variants.append(source)
    source = session()
    nested = source
    for _ in range(arena_video.MAX_DEPTH + 1):
        nested["extra"] = {}
        nested = nested["extra"]
    variants.append(source)
    source = session()
    source["messages"] += [{"id": USER_ID}] * arena_video.MAX_MESSAGES
    variants.append(source)
    for source in variants:
        with pytest.raises(bridge_client.BridgeError):
            arena_video.validate_video_session(source, context())


def test_non_json_values_and_invalid_unicode_do_not_escape_as_raw_exceptions():
    for bad in (object(), float("nan"), 1 << 300, "\ud800"):
        source = session()
        source["extra"] = bad
        with pytest.raises(bridge_client.BridgeError):
            arena_video.validate_video_session(source, context())
    for body in (b"a0:offline", {}, "\ud800"):
        with pytest.raises(bridge_client.BridgeError):
            arena_video.parse_video_stream(body)


@pytest.mark.parametrize("metadata_shape", ["absent", "empty", "no-run"])
def test_native_terminal_success_needs_no_metadata_or_video_run(metadata_shape):
    source = session("success", "success")
    source["pairwiseFeedbacks"] = []
    for message in source["messages"][1:]:
        message.pop("error")
        message.update(reasoning="", parentMessageIds=[USER_ID], failureReason=None)
        if metadata_shape == "absent":
            message.pop("metadata")
        elif metadata_shape == "empty":
            message["metadata"] = {}
        else:
            message["metadata"] = {"unrelated": True}
    result = arena_video.validate_video_session(source, context())
    assert result["completed"] is True
    assert all(lane["run_id"] == "" for lane in result["sides"].values())
    assert_anonymous(result)


def test_session_identity_comes_from_context_not_an_unrelated_page_url():
    source = session("success", "success")
    source["url"] = "https://arena.example.invalid/c/" + OTHER_ID
    checked_context = context()
    checked_context["page_url"] = source["url"]
    result = arena_video.validate_video_session(source, checked_context)
    assert result["evaluation_id"] == EVALUATION_ID
    assert "page_url" not in result["context"]
    # A URL cannot justify substituting a different evaluation object's ID.
    source["id"] = OTHER_ID
    with pytest.raises(bridge_client.BridgeError):
        arena_video.validate_video_session(source, checked_context)


def persisted_flight_session(status_a="success", status_b="success"):
    # Synthetic extracted session shape, NOT a fake evaluation API response.
    source = session(status_a, status_b)
    source.pop("modality")
    source["pairwiseFeedbacks"] = []
    for message in source["messages"][1:]:
        message["metadata"]["videoDuration"] = 5
    return source


def test_verified_flight_session_without_modality_accepts_exact_successful_mp4_sides():
    source = persisted_flight_session()
    checked_context = context()
    result = arena_video.validate_video_session(source, checked_context)
    assert result["completed"] is True
    assert result["sides"]["a"]["run_id"] == RUN_A_ID
    assert result["sides"]["b"]["run_id"] == RUN_B_ID
    assert_anonymous(result)
    # Video attachments, not metadata, establish terminal modality evidence.
    for message in source["messages"][1:]:
        message.pop("metadata")
    assert arena_video.validate_video_session(source, checked_context)["completed"] is True


@pytest.mark.parametrize(
    "statuses",
    [
        ("pending", "pending"),
        ("resampling", "pending"),
        ("success", "resampling"),
    ],
)
def test_missing_modality_pending_requires_each_pending_side_known_video_run(statuses):
    source = persisted_flight_session(*statuses)
    result = arena_video.validate_video_session(source, context())
    assert result["completed"] is False
    for index, side in ((1, "a"), (2, "b")):
        if source["messages"][index]["status"] != "success":
            assert result["sides"][side]["videos"] == []
            source["messages"][index].pop("metadata")
            with pytest.raises(bridge_client.BridgeError):
                arena_video.validate_video_session(source, context())
            break


def test_missing_modality_requires_explicit_video_context_not_default():
    checked_context = context()
    checked_context.pop("modality")
    with pytest.raises(bridge_client.BridgeError):
        arena_video.validate_video_session(persisted_flight_session(), checked_context)
    # The legacy default remains usable when the session itself says video.
    assert arena_video.validate_video_session(session(), checked_context)["completed"] is False


@pytest.mark.parametrize("status", ["success", "failed", "flagged", "stopped"])
def test_missing_modality_run_alone_or_stale_attachment_does_not_prove_video(status):
    source = persisted_flight_session(status, "success")
    if status == "success":
        source["messages"][1]["experimental_attachments"] = []
    with pytest.raises(bridge_client.BridgeError):
        arena_video.validate_video_session(source, context())


@pytest.mark.parametrize("modality", [None, "image", "text", "audio", "", False])
def test_explicit_nonvideo_modality_cannot_be_overridden_by_mp4_or_video_run(modality):
    source = persisted_flight_session()
    source["modality"] = modality
    with pytest.raises(bridge_client.BridgeError):
        arena_video.validate_video_session(source, context())


def test_reported_evaluation_and_terminal_resume_404_are_not_api_success_fixtures():
    for response in (
        {"status": 404, "text": "Not found"},
        {"status": 404, "error": "No active video workflow"},
    ):
        with pytest.raises(bridge_client.BridgeError):
            arena_video.validate_video_session(response, context())
    # Exact successful readback stands independently of continuation availability.
    result = arena_video.validate_video_session(persisted_flight_session(), context())
    assert result["completed"] is True
    assert all(lane["error"] == "" for lane in result["sides"].values())
    assert "terminal resume-workflow 404" in arena_video.__doc__
    assert "JSON evaluation endpoint returned 404" in arena_video.__doc__


def test_module_validates_only_and_labels_live_boundary_honestly():
    assert "validates supplied data only" in arena_video.__doc__
    assert "HTTP 429" in arena_video.__doc__
    assert "did not" in arena_video.__doc__
    assert "prove programmatic generation acceptance or QQ dual-video delivery" in arena_video.__doc__
    assert not hasattr(arena_video, "BattleProtocolMixin")
    assert not hasattr(arena_video, "begin_battle")
    assert not hasattr(arena_video, "fetch")
