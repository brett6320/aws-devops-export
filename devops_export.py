#!/usr/bin/env python3
"""Export AWS DevOps Agent Agent Space tasks (investigations, evaluations, ...)
to static HTML reports.

Read-only: only List*/Get* DevOps Agent APIs are called. Output is written to
local disk (HTML + optional raw JSON); nothing in AWS is modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import truststore

truststore.inject_into_ssl()

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

from journal_view import build_view, markdown  # noqa: E402

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("devops_export")

SERVICE = "devops-agent"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TASK_TYPES = ["INVESTIGATION", "EVALUATION", "RELEASE_READINESS_REVIEW", "RELEASE_TESTING"]
TASK_STATUSES = [
    "PENDING_TRIAGE", "LINKED", "PENDING_START", "IN_PROGRESS", "PENDING_CUSTOMER_APPROVAL",
    "COMPLETED", "FAILED", "TIMED_OUT", "CANCELED", "SKIPPED", "WAITING",
]
PRIORITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "MINIMAL"]

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Execution:
    execution_id: str
    parent_execution_id: str | None
    agent_sub_task: str
    agent_type: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TaskExport:
    task: dict[str, Any]
    executions: list[Execution] = field(default_factory=list)
    recommendations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def task_id(self) -> str:
        return self.task["taskId"]

    @property
    def record_count(self) -> int:
        return sum(len(e.records) for e in self.executions)


# --------------------------------------------------------------------------- #
# AWS access
# --------------------------------------------------------------------------- #
def get_session(profile: str | None, region: str | None) -> boto3.Session:
    # Let boto3 resolve env vars / profile region first; fall back to us-east-1.
    session = boto3.Session(profile_name=profile, region_name=region)
    if session.region_name is None:
        session = boto3.Session(profile_name=profile, region_name="us-east-1")
    return session


def get_client(session: boto3.Session):
    config = Config(retries={"max_attempts": 5, "mode": "adaptive"})
    return session.client(SERVICE, config=config)


def _strip_meta(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k != "ResponseMetadata"}


def list_agent_spaces(client) -> Iterator[dict[str, Any]]:
    log.debug("ListAgentSpaces")
    for page in client.get_paginator("list_agent_spaces").paginate():
        yield from page["agentSpaces"]


def get_agent_space(client, space_id: str) -> dict[str, Any]:
    log.debug("GetAgentSpace %s", space_id)
    return client.get_agent_space(agentSpaceId=space_id)["agentSpace"]


def list_tasks(
    client,
    space_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    status: str | None = None,
    task_type: str | None = None,
    priority: str | None = None,
) -> Iterator[dict[str, Any]]:
    # TaskFilter's status/taskType/priority lists accept exactly one value
    # (model: min=1 max=1), which botocore does not enforce client-side.
    task_filter: dict[str, Any] = {}
    if since:
        task_filter["createdAfter"] = since
    if until:
        task_filter["createdBefore"] = until
    if status:
        task_filter["status"] = [status]
    if task_type:
        task_filter["taskType"] = [task_type]
    if priority:
        task_filter["priority"] = [priority]
    params: dict[str, Any] = {
        "agentSpaceId": space_id,
        "sortField": "CREATED_AT",
        "order": "DESC",
    }
    if task_filter:
        params["filter"] = task_filter
    log.debug("ListBacklogTasks %s", params)
    for page in client.get_paginator("list_backlog_tasks").paginate(**params):
        yield from page["tasks"]


def get_task(client, space_id: str, task_id: str) -> dict[str, Any]:
    log.debug("GetBacklogTask %s", task_id)
    return client.get_backlog_task(agentSpaceId=space_id, taskId=task_id)["task"]


def list_executions(client, space_id: str, task_id: str) -> list[Execution]:
    log.debug("ListExecutions task=%s", task_id)
    out = []
    for page in client.get_paginator("list_executions").paginate(agentSpaceId=space_id, taskId=task_id):
        for e in page["executions"]:
            out.append(
                Execution(
                    execution_id=e["executionId"],
                    parent_execution_id=e.get("parentExecutionId"),
                    agent_sub_task=e["agentSubTask"],
                    agent_type=e.get("agentType"),
                    status=e["executionStatus"],
                    created_at=e["createdAt"],
                    updated_at=e["updatedAt"],
                )
            )
    return order_executions(out)


def order_executions(executions: list[Execution]) -> list[Execution]:
    """Chronological, with each child execution placed right after its parent."""
    executions = sorted(executions, key=lambda e: e.created_at)
    ids = {e.execution_id for e in executions}
    children: dict[str, list[Execution]] = {}
    roots = []
    for e in executions:
        if e.parent_execution_id and e.parent_execution_id in ids:
            children.setdefault(e.parent_execution_id, []).append(e)
        else:
            roots.append(e)
    ordered: list[Execution] = []

    def visit(e: Execution) -> None:
        ordered.append(e)
        for child in children.get(e.execution_id, []):
            visit(child)

    for root in roots:
        visit(root)
    # Parent cycles (never expected) would leave executions unvisited; keep them.
    seen = {e.execution_id for e in ordered}
    ordered.extend(e for e in executions if e.execution_id not in seen)
    return ordered


def list_journal_records(client, space_id: str, execution_id: str) -> list[dict[str, Any]]:
    log.debug("ListJournalRecords execution=%s", execution_id)
    records = []
    paginator = client.get_paginator("list_journal_records")
    for page in paginator.paginate(agentSpaceId=space_id, executionId=execution_id, order="ASC"):
        records.extend(page["records"])
    return records


def list_recommendations(client, space_id: str, task_id: str) -> list[dict[str, Any]]:
    # ListRecommendations has no botocore paginator; follow nextToken by hand.
    log.debug("ListRecommendations task=%s", task_id)
    recs: list[dict[str, Any]] = []
    params: dict[str, Any] = {"agentSpaceId": space_id, "taskId": task_id}
    while True:
        resp = client.list_recommendations(**params)
        recs.extend(resp["recommendations"])
        token = resp.get("nextToken")
        if not token:
            recs.sort(key=lambda r: r.get("rankPosition", sys.maxsize))
            return recs
        params["nextToken"] = token


def collect_task(client, space_id: str, task: dict[str, Any], with_recommendations: bool = True) -> TaskExport:
    export = TaskExport(task=_strip_meta(task))
    export.executions = list_executions(client, space_id, task["taskId"])
    # Some tasks only expose their execution on the task itself.
    known = {e.execution_id for e in export.executions}
    if task.get("executionId") and task["executionId"] not in known:
        export.executions.insert(
            0,
            Execution(
                execution_id=task["executionId"],
                parent_execution_id=None,
                agent_sub_task="(task execution)",
                agent_type=None,
                status="UNKNOWN",
                created_at=task["createdAt"],
                updated_at=task["updatedAt"],
            ),
        )
    for execution in export.executions:
        execution.records = list_journal_records(client, space_id, execution.execution_id)
    if with_recommendations:
        try:
            export.recommendations = list_recommendations(client, space_id, task["taskId"])
        except ClientError as e:
            # Not every task type supports recommendations; don't fail the export over it.
            log.warning(
                "Recommendations unavailable for %s: %s %s",
                task["taskId"], e.response["Error"]["Code"], e.response["Error"]["Message"],
            )
    return export


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _utc(value: datetime) -> datetime:
    # Journal timestamps come back in local time (tzlocal), backlog ones in UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return _utc(o).isoformat()
    return str(o)


def to_json(o: Any) -> str:
    return json.dumps(o, indent=2, default=_json_default, ensure_ascii=False)


def fmt_ts(value: Any) -> str:
    if isinstance(value, datetime):
        return _utc(value).strftime("%Y-%m-%d %H:%M:%S UTC")
    return "" if value is None else str(value)


def safe_url(value: Any) -> str | None:
    """Return the URL only if it is http(s); blocks javascript:/data: links."""
    if not isinstance(value, str):
        return None
    return value if urlparse(value.strip()).scheme.lower() in ("http", "https") else None


def safe_name(value: str) -> str:
    # IDs are <=128 chars; the hash suffix keeps sanitized names unique.
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value)[:128] or "task"
    if cleaned == value:
        return cleaned
    return f"{cleaned}-{hashlib.sha1(value.encode()).hexdigest()[:8]}"


def build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["ts"] = fmt_ts
    env.filters["tojson_pretty"] = to_json
    env.filters["safe_url"] = safe_url
    env.filters["markdown"] = markdown
    env.filters["lower_dash"] = lambda s: str(s or "").lower().replace("_", "-")
    return env


class ReportWriter:
    """Writes each task page as soon as it is collected, then the index."""

    def __init__(self, out_dir: Path, space: dict[str, Any], raw_json: bool, embed_raw: bool = False):
        self.out_dir = out_dir
        self.space = space
        self.raw_json = raw_json
        self.embed_raw = embed_raw
        self.env = build_env()
        self.generated = datetime.now(timezone.utc)
        self.task_tpl = self.env.get_template("task.html.j2")
        (out_dir / "tasks").mkdir(parents=True, exist_ok=True)
        if raw_json:
            (out_dir / "raw").mkdir(exist_ok=True)

    def write_task(self, export: TaskExport) -> None:
        fname = safe_name(export.task_id)
        html = self.task_tpl.render(space=self.space, export=export, view=build_view(export.executions),
                                    embed_raw=self.embed_raw, generated=self.generated)
        (self.out_dir / "tasks" / f"{fname}.html").write_text(html, encoding="utf-8")
        if self.raw_json:
            raw = {
                "task": export.task,
                "executions": [vars(e) for e in export.executions],
                "recommendations": export.recommendations,
            }
            (self.out_dir / "raw" / f"{fname}.json").write_text(to_json(raw), encoding="utf-8")
        log.info("Wrote %s (%d executions, %d records)", fname, len(export.executions), export.record_count)

    def write_index(self, exports: list[TaskExport], failed: list[dict[str, Any]]) -> Path:
        index = self.env.get_template("index.html.j2").render(
            space=self.space,
            exports=exports,
            failed=failed,
            generated=self.generated,
            safe_name=safe_name,
            raw_json=self.raw_json,
        )
        index_path = self.out_dir / "index.html"
        index_path.write_text(index, encoding="utf-8")
        return index_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_date(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid date {value!r} (use YYYY-MM-DD or ISO-8601)") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def upper_choice(choices: list[str]):
    def parse(value: str) -> str:
        item = value.strip().upper()
        if item not in choices:
            raise argparse.ArgumentTypeError(f"invalid value {value!r}; choose one of {','.join(choices)}")
        return item
    return parse


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", default=os.getenv("AWS_PROFILE"), help="AWS profile name")
    common.add_argument("--region", default=os.getenv("AWS_REGION"),
                        help="AWS region (default: AWS_REGION/AWS_DEFAULT_REGION, profile region, else us-east-1)")
    common.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    common.add_argument("-q", "--quiet", action="store_true", help="Warnings and errors only")

    parser = argparse.ArgumentParser(description="Export AWS DevOps Agent Agent Space tasks to HTML reports.")
    sub = parser.add_subparsers(dest="command", required=True)

    ls = sub.add_parser("list-spaces", parents=[common], help="List Agent Spaces in the account/region")
    ls.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    ex = sub.add_parser("export", parents=[common], help="Export tasks from an Agent Space to HTML")
    ex.add_argument("--space-id", default=os.getenv("DEVOPS_AGENT_SPACE_ID"), help="Agent Space ID")
    ex.add_argument("--out", type=Path, default=Path(os.getenv("DEVOPS_EXPORT_OUT", "reports")),
                    help="Output root; reports go to OUT/<space-id>/ (default: reports)")
    ex.add_argument("--task-id", action="append", default=[], help="Export only this task (repeatable)")
    ex.add_argument("--since", type=parse_date, help="Only tasks created at/after this time (inclusive)")
    ex.add_argument("--until", type=parse_date, help="Only tasks created before this time (exclusive)")
    ex.add_argument("--status", type=upper_choice(TASK_STATUSES), help="Single task status")
    ex.add_argument("--task-type", type=upper_choice(TASK_TYPES), help="Single task type")
    ex.add_argument("--priority", type=upper_choice(PRIORITIES), help="Single priority")
    ex.add_argument("--limit", type=positive_int, help="Stop after N tasks (newest first)")
    ex.add_argument("--raw-json", action="store_true", help="Also write raw API data to raw/<task>.json")
    ex.add_argument("--embed-raw", action="store_true",
                    help="Also embed raw journal JSON (collapsed) in each task page")
    ex.add_argument("--no-recommendations", action="store_true", help="Skip ListRecommendations calls")
    ex.add_argument("--dry-run", action="store_true", help="List the tasks that would be exported; write nothing")
    return parser


def cmd_list_spaces(client, args) -> int:
    spaces = [_strip_meta(s) for s in list_agent_spaces(client)]
    if args.json:
        print(to_json(spaces))
        return 0
    if not spaces:
        print("No Agent Spaces found.")
        return 0
    for s in spaces:
        print(f"{s['agentSpaceId']}  {s['name']}  (created {fmt_ts(s['createdAt'])})")
    return 0


def cmd_export(client, args) -> int:
    if not args.space_id:
        log.error("--space-id (or DEVOPS_AGENT_SPACE_ID) is required")
        return 2
    if args.since and args.until and args.since >= args.until:
        log.error("--since must be earlier than --until")
        return 2
    space = _strip_meta(get_agent_space(client, args.space_id))

    if args.task_id:
        task_ids = list(dict.fromkeys(args.task_id))
        tasks = [get_task(client, args.space_id, t) for t in task_ids]
    else:
        tasks = []
        for t in list_tasks(client, args.space_id, args.since, args.until,
                            args.status, args.task_type, args.priority):
            tasks.append(t)
            if args.limit and len(tasks) >= args.limit:
                break
    log.info("Found %d task(s) in space %s (%s)", len(tasks), space["name"], args.space_id)

    if args.dry_run:
        for t in tasks:
            log.info("[DRY-RUN] Would export %s  %s  %s  %s", t["taskId"], t["status"], t["taskType"], t["title"])
        return 0

    writer = ReportWriter(args.out / safe_name(args.space_id), space,
                          raw_json=args.raw_json, embed_raw=args.embed_raw)
    exports: list[TaskExport] = []
    failed: list[dict[str, Any]] = []
    for i, t in enumerate(tasks, 1):
        log.info("[%d/%d] Collecting %s — %s", i, len(tasks), t["taskId"], t["title"])
        try:
            export = collect_task(client, args.space_id, t, with_recommendations=not args.no_recommendations)
        except (ClientError, BotoCoreError) as e:
            reason = (f"{e.response['Error']['Code']}: {e.response['Error']['Message']}"
                      if isinstance(e, ClientError) else str(e))
            log.error("Failed to collect %s: %s", t["taskId"], reason)
            failed.append({"task": _strip_meta(t), "error": reason})
            continue
        try:
            writer.write_task(export)
        except Exception as e:  # unexpected journal shapes must not sink the whole run
            log.exception("Failed to render %s", t["taskId"])
            failed.append({"task": _strip_meta(t), "error": f"render error: {type(e).__name__}: {e}"})
            continue
        exports.append(export)

    index = writer.write_index(exports, failed)
    print(f"Exported {len(exports)} task(s) -> {index}")
    if failed:
        log.error("%d task(s) failed: %s", len(failed), ", ".join(f["task"]["taskId"] for f in failed))
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    try:
        session = get_session(args.profile, args.region)
        log.debug("Using profile=%s region=%s", args.profile or "default", session.region_name)
        client = get_client(session)
        if args.command == "list-spaces":
            return cmd_list_spaces(client, args)
        return cmd_export(client, args)
    except ClientError as e:
        log.error("AWS error %s: %s", e.response["Error"]["Code"], e.response["Error"]["Message"])
        return 1
    except BotoCoreError as e:
        log.error("AWS client error: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
