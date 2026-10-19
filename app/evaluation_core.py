"""Advisory, job-specific candidate evaluation.

What this is: a model compares REDACTED candidate material with the criteria of ONE job and returns
per-criterion scores with evidence. A person reads it and decides.

What this is not:
* not an automated decision — nothing here changes an application's stage, rejects, or hires;
* not a fixed rubric — criteria come from the job (``app/scoring.py``), validated;
* not trusted arithmetic — the overall is computed by the code from per-criterion scores;
* not run silently — it is OFF unless ``KUKHIRE_EVALUATION_ENABLED`` is set, and when it cannot run
  the API says so explicitly instead of answering "queued".

Data sent to the model: the job (title, description, requirements, criteria) and the candidate's
work/skills/projects/education-field text after ``redact_resume``: no name, email, phone, address,
links or profiles, no school or employer-location detail, no dates of birth/graduation. Only a local
provider (Ollama) is wired; no hosted provider is used.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.scoring import CriteriaError, weighted_overall

# Pure and dependency-light on purpose (no web framework, no database): the API service
# (app/evaluation.py) and the command-line tool (score.py) share this one implementation.

REDACTION_VERSION = "1"
_TEMPLATES = Path(__file__).resolve().parent.parent / "prompts" / "templates"
_jinja = Environment(loader=FileSystemLoader(str(_TEMPLATES)), undefined=StrictUndefined, autoescape=False)

MAX_MATERIAL_CHARS = 24_000


class EvaluationUnavailable(Exception):
    """The evaluation cannot run (disabled, no provider, not enough input)."""

    def __init__(self, code: str, message: str, http_status: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class EvaluationFailure(Exception):
    """The model ran but its answer cannot be trusted. `code` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EvaluationProvider(Protocol):
    name: str
    model: str

    def generate(self, system: str, user: str, schema: dict) -> str: ...


class OllamaEvaluationProvider:
    """Local model through Ollama. Imported lazily so the API runs without it installed."""

    name = "ollama"

    def __init__(self, model: str) -> None:
        self.model = model
        try:
            import ollama  # noqa: PLC0415
        except ImportError as exc:
            raise EvaluationUnavailable(
                "provider_unavailable", "The local model runtime (ollama) is not installed on this server."
            ) from exc
        self._ollama = ollama

    def generate(self, system: str, user: str, schema: dict) -> str:
        response = self._ollama.chat(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            format=schema,
            options={"temperature": 0.2},
        )
        return response["message"]["content"]


def default_provider(provider_name: str, model: str) -> EvaluationProvider:
    if provider_name != "ollama":
        raise EvaluationUnavailable(
            "provider_unsupported",
            "Only the local 'ollama' provider is supported for evaluation. Hosted providers are not enabled.",
        )
    return OllamaEvaluationProvider(model)


# --- redaction ------------------------------------------------------------------------------
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def _scrub(text: Any) -> str:
    value = str(text or "")
    value = _EMAIL.sub("[email]", value)
    value = _URL.sub("[link]", value)
    value = _PHONE.sub("[phone]", value)
    return value.strip()


def _years(start: Any, end: Any) -> str:
    """Work duration as years only — enough for 'how long', not a date of birth proxy."""
    s, e = _YEAR.search(str(start or "")), _YEAR.search(str(end or ""))
    if s and e:
        return f"{s.group(0)}-{e.group(0)}"
    if s:
        return f"{s.group(0)}-present"
    return ""


def _lines(items: Any) -> list[str]:
    return [t for t in (_scrub(i) for i in (items or []) if i) if t]


def redact_resume(parsed: dict | None) -> dict:
    """Keep what a role-relevance judgement needs; drop identity, contact and proxy details."""
    parsed = parsed or {}
    out: dict[str, Any] = {}

    basics = parsed.get("basics") or {}
    headline = _scrub(basics.get("label") or "")
    summary = _scrub(basics.get("summary") or "")
    if headline or summary:
        out["headline"] = {"label": headline, "summary": summary}

    work = []
    for w in parsed.get("work") or []:
        work.append(
            {
                "position": _scrub(w.get("position")),
                "period": _years(w.get("startDate"), w.get("endDate")),
                "summary": _scrub(w.get("summary")),
                "highlights": _lines(w.get("highlights")),
            }
        )
    if work:
        out["work"] = work

    volunteer = [
        {"position": _scrub(v.get("position")), "summary": _scrub(v.get("summary")), "highlights": _lines(v.get("highlights"))}
        for v in parsed.get("volunteer") or []
    ]
    if volunteer:
        out["volunteer"] = volunteer

    projects = [
        {
            "name": _scrub(p.get("name")),
            "description": _scrub(p.get("description")),
            "highlights": _lines(p.get("highlights")),
            "keywords": _lines(p.get("keywords")),
        }
        for p in parsed.get("projects") or []
    ]
    if projects:
        out["projects"] = projects

    skills = [{"name": _scrub(s.get("name")), "keywords": _lines(s.get("keywords"))} for s in parsed.get("skills") or []]
    if skills:
        out["skills"] = skills

    # Field of study and level only: no institution, no grades, no dates.
    education = [
        {"area": _scrub(e.get("area")), "studyType": _scrub(e.get("studyType"))} for e in parsed.get("education") or []
    ]
    if education:
        out["education"] = education

    certificates = [{"name": _scrub(c.get("name"))} for c in parsed.get("certificates") or [] if c.get("name")]
    if certificates:
        out["certificates"] = certificates

    return out


def material_text(redacted: dict) -> str:
    text = json.dumps(redacted, ensure_ascii=False, indent=1)
    return text[:MAX_MATERIAL_CHARS]


# --- model output ---------------------------------------------------------------------------
class CriterionResult(BaseModel):
    key: str
    score: float = Field(ge=0, le=10)
    evidence: str = Field(min_length=1, max_length=1200)
    missing_information: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("evidence")
    @classmethod
    def _evidence_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("evidence must not be blank")
        return v.strip()


class EvaluationOutput(BaseModel):
    criteria: list[CriterionResult]
    summary: str = Field(default="", max_length=1500)
    risks: list[str] = Field(default_factory=list, max_length=10)


def _extract_json(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return text


def build_prompts(job: dict, criteria: list[dict], redacted: dict) -> tuple[str, str]:
    system = _jinja.get_template("job_evaluation_system.jinja").render()
    user = _jinja.get_template("job_evaluation_prompt.jinja").render(
        job={
            "title": job.get("title", ""),
            "department": job.get("department"),
            "employment_type": job.get("employment_type"),
            "description": job.get("description", ""),
            "requirements": job.get("requirements"),
        },
        criteria=criteria,
        material=material_text(redacted),
    )
    return system, user




def has_evaluable_content(redacted: dict) -> bool:
    return any(redacted.get(k) for k in ("work", "projects", "skills", "volunteer", "education"))


def evaluate_material(
    job: dict, criteria: list[dict], parsed_resume: dict | None, provider: EvaluationProvider
) -> tuple[EvaluationOutput, float]:
    """Run ONE evaluation: redact, prompt, call the model, validate, compute the overall.

    Raises EvaluationUnavailable("no_resume_content") before the model is called when there is
    nothing to evaluate, and EvaluationFailure for a provider error or an untrustworthy answer.
    Nothing here logs prompts, resume material or model output.
    """
    redacted = redact_resume(parsed_resume)
    if not has_evaluable_content(redacted):
        raise EvaluationUnavailable("no_resume_content", "This candidate has no parsed resume content to evaluate.", 422)
    system, user = build_prompts(job, criteria, redacted)
    try:
        raw = provider.generate(system, user, EvaluationOutput.model_json_schema())
    except EvaluationUnavailable:
        raise
    except Exception:  # provider/network error — the details may echo the prompt, so drop them
        raise EvaluationFailure("provider_error", "The evaluation model could not be reached.")
    try:
        output = EvaluationOutput.model_validate(json.loads(_extract_json(raw)))
    except (ValueError, ValidationError):
        raise EvaluationFailure("invalid_model_output", "The model did not return a valid evaluation. Nothing was recorded as a score.")
    expected = {c["key"] for c in criteria}
    got = [c.key for c in output.criteria]
    if set(got) != expected or len(got) != len(expected):
        raise EvaluationFailure("criteria_mismatch", "The model's answer did not cover exactly this job's criteria.")
    try:
        overall = weighted_overall(criteria, {c.key: c.score for c in output.criteria})
    except CriteriaError:
        raise EvaluationFailure("invalid_model_output", "The model returned scores that could not be combined.")
    return output, overall
