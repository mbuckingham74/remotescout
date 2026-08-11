import types

import pytest

from remotescout import db
from remotescout.app import create_app
from remotescout.discovery import DiscoveredJob
from remotescout.scoring import (
    DEFAULT_MODEL,
    MissingApiKeyError,
    ScoreResult,
    ScoringError,
    build_prompt,
    meets_threshold,
    score_job,
)


def make_job(**overrides):
    fields = {
        "source": "test",
        "source_url": "https://example.com/jobs/1",
        "title": "Technical Program Manager",
        "employer": "Example Co.",
        "description": "Lead cross-functional infrastructure delivery programs.",
        "location": "Remote (US)",
    }
    fields.update(overrides)
    return DiscoveredJob(**fields)


def make_tool_use_message(**fields):
    block = types.SimpleNamespace(type="tool_use", name="score_job_fit", input=fields)
    return types.SimpleNamespace(content=[block])


class FakeMessages:
    def __init__(self, *messages):
        self.responses = list(messages)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class FakeClient:
    def __init__(self, *messages):
        self.messages = FakeMessages(*messages)


def valid_fields(**overrides):
    fields = {
        "score": 84,
        "fit_explanation": "Strong program delivery leadership aligned with the role.",
        "strengths": ["Program governance", "Budget ownership"],
        "gaps": ["No direct fintech domain experience"],
    }
    fields.update(overrides)
    return fields


def test_valid_response_parses():
    client = FakeClient(make_tool_use_message(**valid_fields()))
    result = score_job(make_job(), "resume text", client=client)
    assert isinstance(result, ScoreResult)
    assert result.score == 84
    assert result.fit_explanation == "Strong program delivery leadership aligned with the role."
    assert result.strengths == ["Program governance", "Budget ownership"]
    assert result.gaps == ["No direct fintech domain experience"]
    assert len(client.messages.calls) == 1


def test_valid_first_response_single_request():
    client = FakeClient(make_tool_use_message(**valid_fields()))
    result = score_job(make_job(), "resume", client=client)
    assert result.score == 84
    assert len(client.messages.calls) == 1


def test_invalid_then_valid_second_response_retries_once():
    bad = make_tool_use_message(**valid_fields(strengths=["ok", 5]))
    good = make_tool_use_message(**valid_fields())
    client = FakeClient(bad, good)
    result = score_job(make_job(), "resume", client=client)
    assert result.score == 84
    assert len(client.messages.calls) == 2
    second_messages = client.messages.calls[1]["messages"]
    assert "did not conform to the required output schema" in second_messages[0]["content"]
    assert "score_job_fit" in second_messages[0]["content"] or "Return values" in second_messages[0]["content"]


def test_invalid_then_invalid_raises_after_two_requests():
    client = FakeClient(
        make_tool_use_message(**valid_fields(score=999)),
        make_tool_use_message(**valid_fields(score=-5)),
    )
    with pytest.raises(ScoringError):
        score_job(make_job(), "resume", client=client)
    assert len(client.messages.calls) == 2


def test_malformed_strengths_exercises_retry_path():
    bad = make_tool_use_message(**valid_fields(strengths=[{"text": "not a string"}]))
    good = make_tool_use_message(**valid_fields())
    client = FakeClient(bad, good)
    result = score_job(make_job(), "resume", client=client)
    assert result.score == 84
    assert len(client.messages.calls) == 2


def test_sdk_exception_propagates_without_retry():
    class Boom(Exception):
        pass

    class FailingMessages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            raise Boom("API error")

    client = types.SimpleNamespace(messages=FailingMessages())
    with pytest.raises(Boom):
        score_job(make_job(), "resume", client=client)
    assert len(client.messages.calls) == 1


def test_score_zero_is_valid():
    client = FakeClient(make_tool_use_message(**valid_fields(score=0)))
    assert score_job(make_job(), "resume", client=client).score == 0


def test_score_one_hundred_is_valid():
    client = FakeClient(make_tool_use_message(**valid_fields(score=100)))
    assert score_job(make_job(), "resume", client=client).score == 100


def test_score_below_zero_rejected():
    client = FakeClient(make_tool_use_message(**valid_fields(score=-1)))
    with pytest.raises(ScoringError):
        score_job(make_job(), "resume", client=client)


def test_score_above_one_hundred_rejected():
    client = FakeClient(make_tool_use_message(**valid_fields(score=101)))
    with pytest.raises(ScoringError):
        score_job(make_job(), "resume", client=client)


def test_non_integer_score_rejected():
    client = FakeClient(make_tool_use_message(**valid_fields(score=84.5)))
    with pytest.raises(ScoringError):
        score_job(make_job(), "resume", client=client)
    client = FakeClient(make_tool_use_message(**valid_fields(score=True)))
    with pytest.raises(ScoringError):
        score_job(make_job(), "resume", client=client)
    client = FakeClient(make_tool_use_message(**valid_fields(score="84")))
    with pytest.raises(ScoringError):
        score_job(make_job(), "resume", client=client)


def test_missing_fields_rejected():
    for key in ("score", "fit_explanation", "strengths", "gaps"):
        fields = valid_fields()
        del fields[key]
        client = FakeClient(make_tool_use_message(**fields))
        with pytest.raises(ScoringError):
            score_job(make_job(), "resume", client=client)


def test_malformed_response_rejected():
    client = FakeClient(
        types.SimpleNamespace(content=[types.SimpleNamespace(type="text", name=None, input=None)])
    )
    with pytest.raises(ScoringError):
        score_job(make_job(), "resume", client=client)
    client = FakeClient(make_tool_use_message(**valid_fields(score={"nested": 5})))
    with pytest.raises(ScoringError):
        score_job(make_job(), "resume", client=client)


def test_invalid_json_string_input_rejected():
    block = types.SimpleNamespace(
        type="tool_use", name="score_job_fit", input="{not valid json"
    )
    client = FakeClient(types.SimpleNamespace(content=[block]))
    with pytest.raises(ScoringError):
        score_job(make_job(), "resume", client=client)


def test_strengths_and_gaps_parse():
    client = FakeClient(
        make_tool_use_message(
            **valid_fields(
                strengths=["A", "", "  B  "],
                gaps=[],
            )
        )
    )
    result = score_job(make_job(), "resume", client=client)
    assert result.strengths == ["A", "B"]
    assert result.gaps == []


def test_threshold_helper():
    assert meets_threshold(ScoreResult(70, "x"), 70) is True
    assert meets_threshold(ScoreResult(85, "x"), 70) is True
    assert meets_threshold(ScoreResult(69, "x"), 70) is False
    assert meets_threshold(ScoreResult(70, "x"), 80) is False


def test_missing_api_key_errors_only_on_scoring_call(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("Anthropic client constructed without a key")

    monkeypatch.setattr("anthropic.Anthropic", fail_if_constructed)
    with pytest.raises(MissingApiKeyError, match="ANTHROPIC_API_KEY"):
        score_job(make_job(), "resume")


def test_prompt_contains_resume_and_job():
    prompt = build_prompt(make_job(), "THE RESUMED TEXT")
    user_content = prompt["messages"][0]["content"]
    assert "THE RESUMED TEXT" in user_content
    assert "Lead cross-functional infrastructure delivery programs." in user_content
    assert "Technical Program Manager" in user_content
    assert "Example Co." in user_content
    assert "Remote (US)" in user_content


def test_prompt_instructs_not_to_invent_experience():
    prompt = build_prompt(make_job(), "resume")
    system = prompt["system"]
    assert "not invent experience" in system
    assert "unsupported requirement" in system.lower()
    assert "resume" in system


def test_score_updates_job_without_disturbing_other_fields(tmp_path):
    app = create_app({"DATABASE_PATH": str(tmp_path / "test.db")})
    with app.app_context():
        connection = db.get_db()
        job_id = db.upsert_job(connection, make_job())
        connection.execute(
            "UPDATE jobs SET employer_url = ?, requisition_id = ? WHERE id = ?",
            ("https://example.com/jobs/1", "REQ-42", job_id),
        )
        connection.commit()

        client = FakeClient(make_tool_use_message(**valid_fields()))
        result = score_job(make_job(), "resume", client=client)
        db.set_job_score(connection, job_id, result.score, result.fit_explanation)
        connection.commit()

        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["score"] == 84
        assert row["fit_explanation"] == "Strong program delivery leadership aligned with the role."
        assert row["employer_url"] == "https://example.com/jobs/1"
        assert row["requisition_id"] == "REQ-42"
        assert row["source"] == "test"
        assert row["title"] == "Technical Program Manager"


def test_model_config_passed_to_client(monkeypatch):
    client = FakeClient(make_tool_use_message(**valid_fields()))
    score_job(make_job(), "resume", client=client, model="claude-test-1")
    call = client.messages.calls[0]
    assert call["model"] == "claude-test-1"
    assert call["tool_choice"] == {"type": "tool", "name": "score_job_fit"}
    assert call["tools"][0]["name"] == "score_job_fit"
    assert call["tools"][0]["input_schema"]["required"] == [
        "score",
        "fit_explanation",
        "strengths",
        "gaps",
    ]
    assert DEFAULT_MODEL == "claude-sonnet-5"
