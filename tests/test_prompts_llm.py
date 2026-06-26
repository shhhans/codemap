"""Pure-logic tests for the prompt window and LLM JSON extraction (no network)."""

from __future__ import annotations

from codemap.llm import _extract_json
from codemap.prompts import Candidate, build_window


def test_build_window_lists_candidates_with_signatures() -> None:
    window = build_window(
        flow_type="auth",
        material="authorization header",
        current_node="login",
        current_file="src/auth/controller.py",
        candidates=[
            Candidate(ref="pkg.verifyToken", name="verifyToken", signature="verifyToken(t: str)"),
            Candidate(ref="pkg.log", name="log", signature="log(msg)", file_path="util/log.py"),
        ],
    )
    assert "[当前追踪主线]: auth" in window
    assert "authorization header" in window
    # candidates are numbered so the model can echo ordinals back
    assert "1. pkg.verifyToken" in window
    assert "2. pkg.log" in window
    assert "verifyToken(t: str)" in window


def test_extract_json_plain() -> None:
    assert _extract_json('{"ok": true}') == {"ok": True}


def test_extract_json_strips_code_fence() -> None:
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_recovers_from_surrounding_prose() -> None:
    text = 'Sure! Here is the result: {"decisions": []} hope it helps'
    assert _extract_json(text) == {"decisions": []}
