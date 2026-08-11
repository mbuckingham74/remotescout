import json
from dataclasses import dataclass, field

from remotescout.discovery.models import DiscoveredJob

TOOL_NAME = "score_job_fit"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_OUTPUT_TOKENS = 1024
RETRY_CORRECTION = (
    "Your previous response did not conform to the required output schema. "
    "Return values exactly matching the provided schema."
)

SYSTEM_PROMPT = """\
You are a job-fit evaluator. Score how well a candidate's resume matches a job posting, \
using the provided scoring scale. Return the evaluation using the score_job_fit tool.

Scoring scale:
- 90-100: Exceptional match. The resume directly demonstrates nearly all important \
requirements and responsibilities.
- 80-89: Strong match. Substantial direct evidence, with only minor or reasonably \
transferable gaps.
- 70-79: Good/plausible match. Meaningful alignment, but notable gaps or weaker evidence \
exist.
- 60-69: Weak match. Some relevant experience, but important requirements are unsupported \
or materially different.
- Below 60: Poor match; not worth application effort.

Evaluate evidence, not keyword overlap. Consider:
- Responsibilities versus demonstrated resume experience
- Required experience versus demonstrated experience
- Technical and infrastructure knowledge where relevant
- Product, program, project, and delivery leadership
- Operational and cross-functional leadership
- Seniority and scope
- Relevant domain experience
- Remote and location compatibility when the posting imposes meaningful constraints
- Material requirements for which the resume provides little or no evidence

Distinguish three evidence levels:
1. Direct evidence: the resume clearly demonstrates the experience.
2. Transferable evidence: closely related demonstrated experience reasonably transfers.
3. Unsupported requirement: the resume provides little or no evidence for an important \
requirement.

Do not invent experience that is not present in the resume. Do not penalize the resume for \
using different terminology than the job posting. Important unsupported requirements must \
materially reduce the score. The score is a fit-ranking mechanism, not a prediction of an \
interview or offer. Do not research the employer or search the web; evaluate only the \
provided resume and job posting."""

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": "Report the job-fit evaluation for the candidate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "description": "Fit score from 0 to 100 using the provided rubric.",
            },
            "fit_explanation": {
                "type": "string",
                "description": "2-4 sentence explanation of why the job is or is not a strong fit.",
            },
            "strengths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key demonstrated strengths relevant to the role.",
            },
            "gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Important requirements with little or no resume evidence.",
            },
        },
        "required": ["score", "fit_explanation", "strengths", "gaps"],
    },
}


class ScoringError(Exception):
    pass


class MissingApiKeyError(ScoringError):
    pass


@dataclass
class ScoreResult:
    score: int
    fit_explanation: str
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


def build_prompt(job: DiscoveredJob, resume_text: str, correction=None) -> dict:
    lines = [
        f"Job title: {job.title}",
        f"Employer: {job.employer}",
    ]
    if job.location:
        lines.append(f"Location: {job.location}")
    lines.append("")
    lines.append("Job description:")
    lines.append(job.description or "")
    lines.append("")
    lines.append("Candidate resume:")
    lines.append(resume_text)
    content = "\n".join(lines)
    if correction:
        content = f"{content}\n\n{correction}"
    return {
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}],
    }


def _parse_tool_input(data):
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as error:
            raise ScoringError("Model returned invalid JSON for score result") from error
    if not isinstance(data, dict):
        raise ScoringError("Model returned a non-object score result")
    score = data.get("score")
    if type(score) is not int:
        raise ScoringError("Model returned a non-integer score")
    if not 0 <= score <= 100:
        raise ScoringError(f"Model returned score {score}, outside 0-100")
    explanation = data.get("fit_explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        raise ScoringError("Model returned a missing or empty fit explanation")
    for key in ("strengths", "gaps"):
        values = data.get(key)
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ScoringError(f"Model returned an invalid {key} list")
    return ScoreResult(
        score=score,
        fit_explanation=explanation.strip(),
        strengths=[value.strip() for value in data["strengths"] if value.strip()],
        gaps=[value.strip() for value in data["gaps"] if value.strip()],
    )


def _extract_tool_result(message):
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == TOOL_NAME:
            return _parse_tool_input(block.input)
    raise ScoringError("Model response contained no score_job_fit tool result")


def _score_once(client, model, prompt):
    message = client.messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=prompt["system"],
        messages=prompt["messages"],
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": TOOL_NAME},
    )
    return _extract_tool_result(message)


def score_job(job: DiscoveredJob, resume_text: str, client=None, model=None):
    if client is None:
        from anthropic import Anthropic

        from remotescout.config import load_config

        config = load_config()
        api_key = config["ANTHROPIC_API_KEY"]
        if not api_key:
            raise MissingApiKeyError(
                "ANTHROPIC_API_KEY is not set; cannot score jobs without it"
            )
        client = Anthropic(api_key=api_key)
        model = model or config["ANTHROPIC_MODEL"]
    try:
        return _score_once(client, model or DEFAULT_MODEL, build_prompt(job, resume_text))
    except ScoringError:
        return _score_once(
            client,
            model or DEFAULT_MODEL,
            build_prompt(job, resume_text, correction=RETRY_CORRECTION),
        )


def meets_threshold(result: ScoreResult, threshold: int) -> bool:
    return result.score >= threshold
