# KukHire

Applicant tracking for the Kuklabs ecosystem: workspaces, jobs with their own scoring criteria,
candidates and applications, a validated hiring pipeline, interview scorecards, and a handoff
that asks KukHRMS to create the employee. AI output is **advisory only** and never moves a
candidate.

KukHire is a **separate product** from KukHRMS. KukHRMS keeps employee records, onboarding,
payroll, attendance and leave; KukHire never writes its tables. They meet at one authenticated
HTTP call ([docs/HRMS_HANDOFF.md](docs/HRMS_HANDOFF.md)).

The resume parsing / GitHub enrichment engine (`pdf.py`, `github.py`, `prompts/`) descends from
HackerRank's MIT-licensed *hiring-agent* (see [License](#license)); its fixed scoring rubric has
been removed.

## Contents

- [What exists today](#what-exists-today)
- [Run it](#run-it)
- [Documentation](#documentation)
- [Installation and Setup](#installation-and-setup)
- [Configuration](#configuration)
- [CLI usage](#cli-usage)
- [Contributing](#contributing)
- [License](#license)

## What exists today

Implemented and tested (`GET /v1/capabilities` reports the same list at runtime):
workspaces with `admin / recruiter / interviewer / viewer` roles; jobs with weighted scoring
criteria; candidates (identity) separate from applications; validated pipeline transitions with
history; scorecards; handoff to HRMS; advisory evaluation (off by default, local Ollama only);
a plain-JS recruiter UI; a legacy-data importer.

**Not implemented** (reported as `unavailable`): mailbox ingestion worker, resume upload/parsing
through the API (the parser runs from the command line), Google/phone sign-in in the KukHire UI
(email + password through the shared Kuklabs Account only).

## Run it

```bash
pip install -r requirements-api.txt
export KUKHIRE_AUTH_BASE_URL=https://<your AuthKit host>   # unset => authenticated routes answer 503
python -m app.migrations
uvicorn app.api:app
```

Sign-in is the shared Kuklabs Account: tokens are verified only by calling
`GET {KUKHIRE_AUTH_BASE_URL}/v1/auth/me` (no local JWT validation). Workspace membership and roles
are KukHire's own. Tests: `pip install -r requirements-dev.txt && pytest` (SQLite; set
`KUKHIRE_TEST_DATABASE_URL` for PostgreSQL; browser journeys need `playwright install chromium`).

## Documentation

- [docs/MVP_ARCHITECTURE.md](docs/MVP_ARCHITECTURE.md) - what is built vs not
- [docs/HRMS_HANDOFF.md](docs/HRMS_HANDOFF.md) - the one boundary with KukHRMS
- [docs/MIGRATION_AND_CUTOVER.md](docs/MIGRATION_AND_CUTOVER.md) - schema migrations, legacy import, cutover, rollback
- [docs/EMAIL_SCREENING.md](docs/EMAIL_SCREENING.md) - ingestion contracts
- [AGENTS.md](AGENTS.md) - handoff notes for the next engineer/agent

---

## Installation and Setup

### Prerequisites

- **Python 3.11+**

  The repository pins `.python-version` to 3.11.13.

- **One LLM backend** (either of them)

  - **Ollama** for local models
    Install from the [official site](https://ollama.com/), then run `ollama serve`.
  - **Google Gemini** if you have an API key, get it from [here](https://aistudio.google.com/api-keys).

### Quick setup with pip

```bash
$ git clone https://github.com/interviewstreet/hiring-agent
$ cd hiring-agent

$ python -m venv .venv
# Linux or macOS
$ source .venv/bin/activate
# Windows
# .venv\Scripts\activate

$ pip install -r requirements.txt
```

### Ollama Models

Pull the model you want to use. For example:

```bash
$ ollama pull gemma3:4b
```

If you want different results, you can pull other models such as:

```bash
# For higher system configuration
$ ollama pull gemma3:12b

# For lower system configuration
$ ollama pull gemma3:1b
```

---

## Configuration

Copy `.env.example` to `.env`. API variables are prefixed `KUKHIRE_` (see the file for every one).
The command-line scorer additionally reads `LLM_PROVIDER`, `DEFAULT_MODEL`, `GEMINI_API_KEY`
and `GITHUB_TOKEN`. `KUKHIRE_CLI_DEV_MODE=1` enables on-disk caching of GitHub responses
(personal data) - off by default.

---

## CLI usage

Score one resume against one job's own criteria (there is no built-in rubric):

```bash
python score.py resume.pdf --job job.json [--out result.json]
```

Import legacy KukBook recruitment data: see [docs/MIGRATION_AND_CUTOVER.md](docs/MIGRATION_AND_CUTOVER.md).

---

## Contributing

Please read the [CONTRIBUTING.md](./CONTRIBUTING.md) for detailed guidelines on filing issues, proposing changes, and submitting pull requests. Key principles include:

- Keep prompts declarative and provider-agnostic.
- Validate changes with a couple of real resumes under different providers.
- Add or adjust unit-free smoke tests that call each stage with minimal inputs.

---


## License

[MIT](https://github.com/interviewstreet/hiring-agent/blob/master/LICENSE) © HackerRank
