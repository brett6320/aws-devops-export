"""Offline tests using botocore's Stubber, which checks request/response member
names, types and required fields against the real devops-agent service model
(not min/max/pattern constraints)."""
from __future__ import annotations

from datetime import datetime, timezone

import boto3
import pytest
from botocore.stub import ANY, Stubber

import devops_export as dx

SPACE = "space-123"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 1, 12, 30, tzinfo=timezone.utc)


@pytest.fixture
def client():
    c = boto3.client(
        "devops-agent", region_name="us-east-1",
        aws_access_key_id="x", aws_secret_access_key="x",
    )
    with Stubber(c) as stub:
        c.stub = stub
        yield c
        stub.assert_no_pending_responses()


def space_resp():
    return {"agentSpace": {"agentSpaceId": SPACE, "name": "prod <ops>", "createdAt": T0, "updatedAt": T0}}


def task(task_id="task-1", execution_id="exec-1", **kw):
    t = {
        "agentSpaceId": SPACE, "taskId": task_id, "executionId": execution_id,
        "title": "High 5xx on checkout <api>", "description": "ALB 5xx spike",
        "taskType": "INVESTIGATION", "priority": "HIGH", "status": "COMPLETED",
        "createdAt": T0, "updatedAt": T1, "version": 1,
    }
    t.update(kw)
    return t


def stub_task_detail(stub, task_id="task-1", execution_id="exec-1"):
    stub.add_response(
        "list_executions",
        {"executions": [{
            "agentSpaceId": SPACE, "executionId": execution_id, "agentSubTask": "root-cause-analysis",
            "createdAt": T0, "updatedAt": T1, "executionStatus": "STOPPED", "agentType": "investigator",
        }]},
        {"agentSpaceId": SPACE, "taskId": task_id},
    )
    stub.add_response(
        "list_journal_records",
        {"records": [
            {"agentSpaceId": SPACE, "executionId": execution_id, "recordId": "r1",
             "content": {"text": "Root cause: <script>alert(1)</script> bad deploy"},
             "createdAt": T0, "recordType": "finding"},
            {"agentSpaceId": SPACE, "executionId": execution_id, "recordId": "r2",
             "content": {"tool": "cloudwatch", "args": {"metric": "5xx"}},
             "createdAt": T1, "recordType": "tool_call"},
        ], "nextToken": "page2"},
        {"agentSpaceId": SPACE, "executionId": execution_id, "order": "ASC"},
    )
    stub.add_response(
        "list_journal_records",
        {"records": [
            {"agentSpaceId": SPACE, "executionId": execution_id, "recordId": "r3",
             "content": {"message": {"markdown": "Rolled back."}},
             "createdAt": T1, "recordType": "summary"},
        ]},
        {"agentSpaceId": SPACE, "executionId": execution_id, "order": "ASC", "nextToken": "page2"},
    )
    stub.add_response(
        "list_recommendations",
        {"recommendations": [
            {"agentSpaceArn": "arn:aws:aidevops:us-east-1:1:agentspace/x", "recommendationId": "rec-2",
             "taskId": task_id, "title": "Second", "content": {"summary": "b"}, "status": "PROPOSED",
             "priority": "LOW", "rankPosition": 2, "createdAt": T0, "updatedAt": T0, "version": 1},
            {"agentSpaceArn": "arn:aws:aidevops:us-east-1:1:agentspace/x", "recommendationId": "rec-1",
             "taskId": task_id, "title": "Add canary alarm", "content": {"summary": "a", "spec": "yaml"},
             "status": "PROPOSED", "priority": "HIGH", "rankPosition": 1, "createdAt": T0, "updatedAt": T0,
             "version": 1},
        ]},
        {"agentSpaceId": SPACE, "taskId": task_id},
    )


def test_collect_task_paginates_and_sorts(client):
    stub_task_detail(client.stub)
    export = dx.collect_task(client, SPACE, task())
    assert [e.execution_id for e in export.executions] == ["exec-1"]
    assert [r["recordId"] for r in export.executions[0].records] == ["r1", "r2", "r3"]
    assert [r["recommendationId"] for r in export.recommendations] == ["rec-1", "rec-2"]


def test_collect_task_tolerates_recommendation_error(client):
    s = client.stub
    s.add_response("list_executions", {"executions": []}, {"agentSpaceId": SPACE, "taskId": "task-1"})
    s.add_response("list_journal_records", {"records": []},
                   {"agentSpaceId": SPACE, "executionId": "exec-1", "order": "ASC"})
    s.add_client_error("list_recommendations", "ValidationException", "not supported")
    export = dx.collect_task(client, SPACE, task())
    # Execution only known from the task itself is still exported.
    assert [e.execution_id for e in export.executions] == ["exec-1"]
    assert export.recommendations == []


def test_end_to_end_export(client, tmp_path, monkeypatch):
    s = client.stub
    s.add_response("get_agent_space", space_resp(), {"agentSpaceId": SPACE})
    s.add_response(
        "list_backlog_tasks",
        {"tasks": [task()]},
        {"agentSpaceId": SPACE, "sortField": "CREATED_AT", "order": "DESC",
         "filter": {"createdAfter": ANY, "taskType": ["INVESTIGATION"]}},
    )
    stub_task_detail(s)
    monkeypatch.setattr(dx, "get_client", lambda session: client)

    rc = dx.main([
        "export", "--space-id", SPACE, "--out", str(tmp_path),
        "--since", "2026-10-01", "--task-type", "investigation", "--raw-json",
    ])
    assert rc == 0

    out = tmp_path / SPACE
    index = (out / "index.html").read_text()
    page = (out / "tasks" / "task-1.html").read_text()
    assert (out / "raw" / "task-1.json").exists()

    assert 'href="tasks/task-1.html"' in index
    assert "prod &lt;ops&gt;" in index
    # Untrusted journal content is escaped, never injected.
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "Rolled back." in page
    assert page.index("Add canary alarm") < page.index("Second")


def test_dry_run_writes_nothing(client, tmp_path, monkeypatch):
    s = client.stub
    s.add_response("get_agent_space", space_resp(), {"agentSpaceId": SPACE})
    s.add_response("list_backlog_tasks", {"tasks": [task(), task("task-2")]},
                   {"agentSpaceId": SPACE, "sortField": "CREATED_AT", "order": "DESC"})
    monkeypatch.setattr(dx, "get_client", lambda session: client)
    assert dx.main(["export", "--space-id", SPACE, "--out", str(tmp_path), "--dry-run"]) == 0
    assert list(tmp_path.iterdir()) == []


def test_limit_stops_early(client, tmp_path, monkeypatch):
    s = client.stub
    s.add_response("get_agent_space", space_resp(), {"agentSpaceId": SPACE})
    s.add_response("list_backlog_tasks", {"tasks": [task(), task("task-2")], "nextToken": "more"},
                   {"agentSpaceId": SPACE, "sortField": "CREATED_AT", "order": "DESC"})
    monkeypatch.setattr(dx, "get_client", lambda session: client)
    assert dx.main(["export", "--space-id", SPACE, "--out", str(tmp_path), "--dry-run", "--limit", "1"]) == 0


@pytest.mark.parametrize("argv", [
    ["export", "--status", "BOGUS"],
    # TaskFilter lists are max=1 server-side; botocore won't catch it.
    ["export", "--status", "COMPLETED,FAILED"],
    ["export", "--limit", "0"],
])
def test_invalid_args_rejected(argv):
    with pytest.raises(SystemExit):
        dx.build_parser().parse_args(argv)


def test_filters_send_single_values(client):
    client.stub.add_response(
        "list_backlog_tasks", {"tasks": []},
        {"agentSpaceId": SPACE, "sortField": "CREATED_AT", "order": "DESC",
         "filter": {"status": ["FAILED"], "taskType": ["INVESTIGATION"], "priority": ["HIGH"]}},
    )
    assert list(dx.list_tasks(client, SPACE, status="FAILED", task_type="INVESTIGATION", priority="HIGH")) == []


def test_safe_url():
    assert dx.safe_url("https://jira.example.com/browse/X-1") == "https://jira.example.com/browse/X-1"
    assert dx.safe_url("javascript:alert(1)") is None
    assert dx.safe_url(" JavaScript:alert(1)") is None
    assert dx.safe_url("data:text/html,x") is None
    assert dx.safe_url(None) is None


def test_safe_name_unique():
    assert dx.safe_name("task-1") == "task-1"
    assert dx.safe_name("a/b") != dx.safe_name("a:b")
    long_a, long_b = "x" * 127 + "/", "x" * 127 + ":"
    assert dx.safe_name(long_a) != dx.safe_name(long_b)


def test_order_executions_groups_children():
    def ex(i, parent, minute):
        return dx.Execution(i, parent, "s", None, "STOPPED", T0.replace(minute=minute), T1)
    ordered = dx.order_executions([ex("root", None, 0), ex("other", None, 1), ex("child", "root", 2),
                                   ex("orphan", "gone", 3)])
    assert [e.execution_id for e in ordered] == ["root", "child", "other", "orphan"]
    cyclic = dx.order_executions([ex("a", "b", 0), ex("b", "a", 1), ex("c", "c", 2), ex("d", None, 3)])
    assert sorted(e.execution_id for e in cyclic) == ["a", "b", "c", "d"]


def test_javascript_reference_url_not_linked(client, tmp_path, monkeypatch):
    s = client.stub
    bad = task(reference={"system": "jira", "title": "J-1", "referenceId": "J-1",
                          "referenceUrl": "javascript:alert(document.domain)", "associationId": "a"})
    s.add_response("get_agent_space", space_resp(), {"agentSpaceId": SPACE})
    s.add_response("get_backlog_task", {"task": bad}, {"agentSpaceId": SPACE, "taskId": "task-1"})
    stub_task_detail(s)
    monkeypatch.setattr(dx, "get_client", lambda session: client)
    assert dx.main(["export", "--space-id", SPACE, "--out", str(tmp_path), "--task-id", "task-1",
                    "--task-id", "task-1"]) == 0
    page = (tmp_path / SPACE / "tasks" / "task-1.html").read_text()
    assert 'href="javascript:' not in page
    assert "Content-Security-Policy" in page
    # Duplicate --task-id collapses to one row.
    assert (tmp_path / SPACE / "index.html").read_text().count('href="tasks/task-1.html"') == 1


def test_failed_task_does_not_abort_run(client, tmp_path, monkeypatch):
    s = client.stub
    s.add_response("get_agent_space", space_resp(), {"agentSpaceId": SPACE})
    s.add_response("list_backlog_tasks", {"tasks": [task("bad", "exec-bad"), task()]},
                   {"agentSpaceId": SPACE, "sortField": "CREATED_AT", "order": "DESC"})
    s.add_client_error("list_executions", "AccessDeniedException", "nope")
    stub_task_detail(s)
    monkeypatch.setattr(dx, "get_client", lambda session: client)
    assert dx.main(["export", "--space-id", SPACE, "--out", str(tmp_path)]) == 3
    out = tmp_path / SPACE
    assert (out / "tasks" / "task-1.html").exists()
    assert not (out / "tasks" / "bad.html").exists()
    index = (out / "index.html").read_text()
    assert "AccessDeniedException" in index


def test_missing_space_id(monkeypatch):
    monkeypatch.delenv("DEVOPS_AGENT_SPACE_ID", raising=False)
    monkeypatch.setattr(dx, "get_client", lambda session: None)
    assert dx.main(["export", "--space-id", ""]) == 2


def test_region_resolution(monkeypatch, tmp_path):
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    assert dx.get_session(None, None).region_name == "us-east-1"
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    assert dx.get_session(None, None).region_name == "eu-west-1"
    assert dx.get_session(None, "ap-south-1").region_name == "ap-south-1"
    # botocore ignores AWS_REGION, so the CLI reads it explicitly.
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert dx.build_parser().parse_args(["list-spaces"]).region == "us-west-2"


def test_render_failure_does_not_abort_run(client, tmp_path, monkeypatch):
    s = client.stub
    s.add_response("get_agent_space", space_resp(), {"agentSpaceId": SPACE})
    s.add_response("list_backlog_tasks", {"tasks": [task()]},
                   {"agentSpaceId": SPACE, "sortField": "CREATED_AT", "order": "DESC"})
    stub_task_detail(s)
    monkeypatch.setattr(dx, "get_client", lambda session: client)
    monkeypatch.setattr(dx, "build_view", lambda executions: 1 / 0)
    assert dx.main(["export", "--space-id", SPACE, "--out", str(tmp_path)]) == 3
    index = (tmp_path / SPACE / "index.html").read_text()
    assert "render error: ZeroDivisionError" in index
