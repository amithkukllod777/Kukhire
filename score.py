"""Evaluate ONE resume (PDF) against ONE job, from the command line.

    python score.py resume.pdf --job job.json [--out result.json] [--cache-dir DIR]

``job.json``::

    {"title": "...", "description": "...", "requirements": "...",
     "scoring_criteria": [{"key": "python", "label": "Python depth", "weight": 60, "description": "..."}]}

What it does: extracts structured data from the PDF (the existing parser), then scores it against
the job's OWN criteria through the same advisory evaluation the API uses
(``app/evaluation_core.py``): identity and contact details are removed before the model sees
anything, scores come with evidence, the overall is computed here, and nothing is a decision.

There is no built-in rubric: without a job file with criteria, it refuses to score. Only a local
model is used for scoring. Nothing is written to disk unless you ask (``--out``, ``--cache-dir``).
"""
import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from app.evaluation_core import (
    EvaluationFailure,
    EvaluationUnavailable,
    default_provider,
    evaluate_material,
)
from app.scoring import CriteriaError, validate_criteria
from models import JSONResume, ModelProvider
from pdf import PDFHandler
from prompt import DEFAULT_MODEL, MODEL_PROVIDER_MAPPING

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def load_job(path: str) -> dict:
    job = json.loads(Path(path).read_text(encoding="utf-8"))
    for field in ("title", "description"):
        if not str(job.get(field, "")).strip():
            raise SystemExit(f"job file needs a non-empty '{field}'")
    try:
        job["scoring_criteria"] = validate_criteria(job.get("scoring_criteria"))
    except CriteriaError as exc:
        raise SystemExit(f"job file scoring_criteria is invalid: {exc}")
    return job


def is_valid_resume_data(resume_data: Optional[JSONResume]) -> bool:
    """True if the extraction produced at least some core content."""
    if not resume_data:
        return False
    return any(
        section is not None
        for section in (
            resume_data.basics,
            resume_data.work,
            resume_data.education,
            resume_data.skills,
            resume_data.projects,
        )
    )


def extract_resume(pdf_path: str, cache_dir: Optional[str]) -> Optional[JSONResume]:
    cache_file = None
    if cache_dir:
        # Opt-in. The cache holds the candidate's parsed resume, so it is never created by default.
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        cache_file = Path(cache_dir) / f"resume_{Path(pdf_path).stem}.json"
        if cache_file.exists():
            try:
                cached = JSONResume(**json.loads(cache_file.read_text(encoding="utf-8")))
                if is_valid_resume_data(cached):
                    return cached
            except Exception:
                logger.warning("Ignoring an unreadable cache file; re-extracting.")
    resume = PDFHandler().extract_json_from_pdf(pdf_path)
    if cache_file and resume and is_valid_resume_data(resume):
        cache_file.write_text(json.dumps(resume.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
    return resume


def print_result(result: dict) -> None:
    print("\nADVISORY EVALUATION — a person decides; this is not a hiring decision")
    print("=" * 72)
    print(f"Job: {result['job']}    Model: {result['model']}")
    print(f"Weighted overall (0-10): {result['overall']}\n")
    for c in result["criteria"]:
        print(f"- {c['label']} (weight {c['weight']:g}): {c['score']:g}/10")
        print(f"    evidence: {c['evidence']}")
        if c["missing_information"]:
            print(f"    missing: {'; '.join(c['missing_information'])}")
    if result["summary"]:
        print(f"\nSummary: {result['summary']}")
    for risk in result["risks"]:
        print(f"Risk: {risk}")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pdf", help="resume PDF")
    parser.add_argument("--job", required=True, help="job JSON with title, description and scoring_criteria")
    parser.add_argument("--out", help="write the result JSON here")
    parser.add_argument("--cache-dir", help="cache the parsed resume here (off by default: it is personal data)")
    args = parser.parse_args(argv)

    if not Path(args.pdf).exists():
        print(f"Error: File '{args.pdf}' does not exist.", file=sys.stderr)
        return 1
    if MODEL_PROVIDER_MAPPING.get(DEFAULT_MODEL) == ModelProvider.GEMINI:
        print("Evaluation uses a local model only; set DEFAULT_MODEL to an Ollama model.", file=sys.stderr)
        return 1

    job = load_job(args.job)
    resume = extract_resume(args.pdf, args.cache_dir)
    if not is_valid_resume_data(resume):
        print("Could not extract usable content from this PDF.", file=sys.stderr)
        return 1

    try:
        provider = default_provider("ollama", DEFAULT_MODEL)
        output, overall = evaluate_material(job, job["scoring_criteria"], resume.model_dump(), provider)
    except (EvaluationUnavailable, EvaluationFailure) as exc:
        print(f"Evaluation not produced: {exc.message}", file=sys.stderr)
        return 2

    by_key = {c["key"]: c for c in job["scoring_criteria"]}
    result = {
        "job": job["title"],
        "model": provider.model,
        "advisory": True,
        "overall": overall,
        "criteria": [
            {
                "key": c.key,
                "label": by_key[c.key]["label"],
                "weight": by_key[c.key]["weight"],
                "score": c.score,
                "evidence": c.evidence,
                "missing_information": c.missing_information,
            }
            for c in output.criteria
        ],
        "summary": output.summary,
        "risks": output.risks,
    }
    print_result(result)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
