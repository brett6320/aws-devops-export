# aws-devops-export

[![Tests](https://github.com/brett6320/aws-devops-export/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/brett6320/aws-devops-export/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)
![Platforms](https://img.shields.io/badge/platforms-linux%20%7C%20macos%20%7C%20windows-lightgrey)

Exports tasks from an AWS DevOps Agent **Agent Space** (investigations, evaluations, release-readiness reviews, release testing) to static, self-contained HTML reports. For each task it pulls the task metadata, every agent execution, the full journal (`ListJournalRecords`) and any recommendations, then renders one page per task plus an `index.html`. The AWS console has no export feature, so use this for incident write-ups, audits or offline archives.

> **Status:** tested offline against the botocore service model only, not yet run against a live Agent Space.

The tool is **read-only**: it calls only `List*`/`Get*` DevOps Agent APIs and writes files to local disk.

## Usage

```bash
# List Agent Spaces in the account/region
python devops_export.py list-spaces [--json]

# Preview which tasks would be exported (writes nothing)
python devops_export.py export --space-id <id> --dry-run

# Export everything in a space
python devops_export.py export --space-id <id>

# Failed investigations from November, plus raw JSON
python devops_export.py export --space-id <id> --since 2026-11-01 --until 2026-12-01 \
    --task-type INVESTIGATION --status FAILED --raw-json

# Specific tasks only
python devops_export.py export --space-id <id> --task-id <task-1> --task-id <task-2>
```

Open `reports/<space-id>/index.html` in a browser. Pages have no external assets, so they work offline and can be zipped or emailed. They follow light/dark mode and print cleanly, with collapsed sections expanded when printed.

### Options

Common to both subcommands:

| Flag | Description |
|---|---|
| `--profile NAME` | AWS profile (overrides `AWS_PROFILE`) |
| `--region REGION` | AWS region. Default: `AWS_REGION`/`AWS_DEFAULT_REGION`, then the profile's region, then `us-east-1` |
| `-v` / `--verbose` | Debug logging (logs each API call) |
| `-q` / `--quiet` | Warnings and errors only |

`export`:

| Flag | Description |
|---|---|
| `--space-id ID` | Agent Space ID (or `DEVOPS_AGENT_SPACE_ID`) |
| `--out DIR` | Output root; reports go to `DIR/<space-id>/` (default `reports`) |
| `--task-id ID` | Export only this task; repeatable, duplicates ignored. Ignores the filters below. If an ID can't be fetched, the run stops before writing anything |
| `--since DATE` / `--until DATE` | Created-at window, `YYYY-MM-DD` or ISO-8601 (UTC if no offset). `--since` is inclusive and `--until` is **exclusive**: `--until 2026-12-01` includes all of Nov 30 |
| `--status S` | One status: `PENDING_TRIAGE`, `LINKED`, `PENDING_START`, `IN_PROGRESS`, `PENDING_CUSTOMER_APPROVAL`, `COMPLETED`, `FAILED`, `TIMED_OUT`, `CANCELED`, `SKIPPED`, `WAITING` |
| `--task-type T` | One type: `INVESTIGATION`, `EVALUATION`, `RELEASE_READINESS_REVIEW`, `RELEASE_TESTING` |
| `--priority P` | One priority: `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `MINIMAL` |
| `--limit N` | Stop after N tasks (newest first) |
| `--raw-json` | Also write the raw API data to `raw/<task-id>.json` |
| `--embed-raw` | Also embed each execution's raw journal JSON, collapsed, in the task page. Off by default because it roughly 8× the page size |
| `--no-recommendations` | Skip `ListRecommendations` |
| `--dry-run` | List matching tasks; write nothing |

The API accepts only **one** value for each of `--status`, `--task-type` and `--priority`. To export several statuses, run the tool once per status.

## Output

```
reports/<space-id>/
├── index.html          # task table: created, title, type, priority, status, record count
├── tasks/<task-id>.html
└── raw/<task-id>.json  # only with --raw-json
```

Each task page is laid out for people to read rather than as a raw dump:

1. **Header:** status, priority, type, timestamps and the linked ticket.
2. **Investigation / mitigation summary:** the agent's final markdown summaries, rendered as formatted text.
3. **Symptoms, findings, observations, investigation gaps:** one card each with its title and description. Extra fields (time range, related resources and so on) are in a collapsed *Details* block. If a fact was updated during the run, only its latest version is shown.
4. **Recommendations**
5. **Live summary:** the final state of the console's summary panel, collapsed.
6. **Agent timeline:** per execution, the prompt, the agent's replies, its reasoning (collapsed) and every tool call paired with its result, collapsed under a one-line label such as `use_aws: sso-admin list_instances · us-east-1`. Tool output over 20k characters is truncated; use `--raw-json` to get it in full.
7. **Agent resource usage:** context-window use and calls per tool, from the last `utilization` record. The many intermediate `utilization` records are not shown.

Journal `content` usually arrives as JSON encoded inside a string. The tool decodes it, renders known record types as shown above and shows unknown types as labeled fields instead of JSON. Markdown is rendered by a small built-in renderer that escapes everything first, so raw HTML in agent output is shown as text. Only `http(s)` links become clickable, and every page sets a Content-Security-Policy that blocks scripts and external loads.

If a task can't be collected (for example AccessDenied or throttling), the tool logs it, keeps going, lists it under **Failed to export** in `index.html` and exits with code `3`. Exit codes: `0` ok, `1` AWS/client error, `2` bad arguments, `3` some tasks failed.

Re-running into the same `--out` overwrites pages for the same tasks but doesn't delete pages from earlier runs. Use a fresh directory for a clean export.

Logs go to stderr. The final summary line goes to stdout. `list-spaces --json` prints JSON to stdout.

## Requirements

- Python 3.10+ (truststore requirement)
- boto3/botocore **1.44.3+** (the version tested), which includes the `devops-agent` client (API `2026-01-01`)
- AWS credentials (profile, env vars, SSO or IAM role)

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # runtime
.venv/bin/pip install -r requirements-dev.txt    # + pytest
```

## Configuration

```bash
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `AWS_PROFILE` | (default chain) | AWS profile name |
| `AWS_REGION` | `us-east-1` | Region of the Agent Space |
| `DEVOPS_AGENT_SPACE_ID` | — | Default `--space-id` |
| `DEVOPS_EXPORT_OUT` | `reports` | Default `--out` |

Precedence: CLI flags > shell environment > `.env` > built-in defaults. `.env` doesn't override variables already set in your shell. It is found by searching upward from the script's directory.

## IAM Permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "aidevops:ListAgentSpaces",
        "aidevops:GetAgentSpace",
        "aidevops:ListBacklogTasks",
        "aidevops:GetBacklogTask",
        "aidevops:ListExecutions",
        "aidevops:ListJournalRecords",
        "aidevops:ListRecommendations"
      ],
      "Resource": "*"
    }
  ]
}
```

The IAM prefix is assumed from the service's signing name (`aidevops`). Check it against the service authorization reference before you rely on this policy. Scope `Resource` to the Agent Space ARN where you can.

## Tests

```bash
.venv/bin/python -m pytest
```

The tests are offline. They use `botocore.stub.Stubber`, which checks request and response member names, types and required fields against the real `devops-agent` service model. It doesn't check min/max, pattern or enum constraints. moto doesn't cover this service.

## Platform Notes

- **macOS**: uses the system trust store (Security.framework) via `truststore`. No certificate setup needed.
- **Windows**: uses the Windows Certificate Store. Credentials are in `%USERPROFILE%\.aws\credentials`. Run with `.venv\Scripts\python devops_export.py ...`.
- **Linux**: uses the system CA bundle via `truststore`. Make sure `ca-certificates` is installed.
