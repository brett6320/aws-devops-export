from __future__ import annotations

import json
from datetime import datetime, timezone

import devops_export as dx
import journal_view as jv

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def rec(record_type, content, i=0):
    # Real records carry content as a JSON *string* inside the JSON document.
    return {"recordId": f"r{i}", "recordType": record_type, "createdAt": T0,
            "content": content if isinstance(content, str) else json.dumps(content)}


def execution(records):
    return dx.Execution("exec-1", None, "investigation", None, "STOPPED", T0, T0, records)


def test_decode_double_encoded():
    assert jv.decode_content(json.dumps(json.dumps({"a": 1}))) == {"a": 1}
    assert jv.decode_content("# heading") == "# heading"
    assert jv.decode_content("{not json") == "{not json"


def test_markdown_basics_and_escaping():
    out = str(jv.markdown(
        "# Title\n\nSome **bold** and `code <x>` and <script>alert(1)</script>\n\n"
        "- a\n  - nested\n- b\n\n1. one\n2. two\n\n| H1 | H2 |\n|:--|--|\n| x | y |\n\n```\n<b>raw</b>\n```\n\n"
        "[ok](https://example.com) [bad](javascript:alert(1))"
    ))
    assert "<h3>Title</h3>" in out
    assert "<strong>bold</strong>" in out
    assert "<code>code &lt;x&gt;</code>" in out
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert out.count("<ul>") == 2 and "<ol>" in out
    assert "<th>H1</th>" in out and "<td>y</td>" in out
    assert "&lt;b&gt;raw&lt;/b&gt;" in out
    assert '<a href="https://example.com"' in out
    assert 'href="javascript' not in out


def test_render_value_no_raw_json():
    out = str(jv.render_value({"start_time": "2026-11-15", "related_resources": ["a", "b"], "nested": {"k": "<v>"}}))
    assert "Start time" in out and "a, b" in out and "&lt;v&gt;" in out
    assert "{" not in out


def test_build_view():
    records = [
        rec("message", {"role": "user", "content": [{"type": "text", "text": "Why is SSO down?"}]}, 0),
        rec("message", {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "check health", "signature": "SECRETSIG"},
            {"type": "tool_use", "id": "t1", "tool_name": "datetime", "input": {"expression": "now"}},
        ]}, 1),
        rec("message", {"role": "user", "content": [
            {"type": "tool_result", "id": "t1", "status": "success", "content": [{"text": '{"now": "12:00"}'}]},
        ]}, 2),
        rec("message", {"role": "assistant", "content": [{"type": "text", "text": "It is an **AWS** event."}]}, 3),
        rec("utilization", {"data": {"context_window": {"utilization": 1.0}, "tools": []}}, 4),
        rec("utilization", {"data": {"context_window": {"utilization": 9.5, "compaction_count": 0},
                                     "tools": [{"name": "datetime", "tool_use_count": 1}]}}, 5),
        rec("finding", {"type": "finding", "id": "f1", "title": "Old", "description": "old"}, 6),
        rec("finding", {"type": "finding", "id": "f1", "title": "Health event", "description": "AWS-side",
                        "related_resources": ["IAM Identity Center"]}, 7),
        rec("investigation_summary", {"type": "investigation_summary", "symptoms": []}, 8),
        rec("investigation_summary_md", "# Investigation Summary\n\n## Findings", 9),
        rec("ui_investigation_summary", {"type": "ui_investigation_summary", "content": {
            "type": "container", "children": [{"type": "title", "props": {"level": 1}, "text": "Done"}]}}, 10),
        rec("brand_new_type", {"foo": "bar"}, 11),
    ]
    view = jv.build_view([execution(records)])
    kinds = [e.kind for e in view.timelines["exec-1"]]
    assert kinds == ["prompt", "thinking", "tool", "assistant", "other"]
    tool = view.timelines["exec-1"][2].data
    assert tool["label"] == "datetime(now)" and tool["status"] == "success"
    assert '"now": "12:00"' in tool["result"]
    assert "SECRETSIG" not in str(view.timelines["exec-1"][1].data["html"])
    assert view.usage["context"] == 9.5 and view.usage["tools"][0]["name"] == "datetime"
    assert [f["title"] for f in view.facts["Findings"]] == ["Health event"]
    assert "IAM Identity Center" in str(view.facts["Findings"][0]["extra"])
    assert view.summaries[0][0] == "Investigation summary"
    assert "<h3>Investigation Summary</h3>" in str(view.summaries[0][1])
    assert "Done" in str(view.ui_summary)
    assert view.record_types["message"] == 4


def test_tool_labels():
    assert jv._tool_call_label("use_aws", {"service_name": "sso-admin", "operation_name": "ListInstances",
                                           "aws_region": "us-east-1", "parameters": {}}) == \
        "use_aws: sso-admin ListInstances · us-east-1"
    assert jv._tool_call_label("fs_read", {"file_path": "/a/SKILL.md"}) == "fs_read(/a/SKILL.md)"
    assert jv._tool_call_label("list_memory_stores", {}) == "list_memory_stores"


def test_json_prompt_rendered_as_fields():
    payload = json.dumps(["Active events", {"event": {"service": "MULTIPLE_SERVICES"}}])
    msg = rec("message", {"role": "user", "content": [{"type": "text", "text": payload}]})
    html_out = str(jv.build_view([execution([msg])]).timelines["exec-1"][0].data["html"])
    assert "MULTIPLE_SERVICES" in html_out and '{"event"' not in html_out


def test_tool_use_id_pairing_and_unpaired_results():
    records = [
        rec("message", {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "shell", "input": {"cmd": "ls"}}]}, 0),
        rec("message", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": [{"text": "file.txt"}]},
            {"type": "tool_result", "toolUseId": "orphan", "content": [{"text": "lost output"}]}]}, 1),
    ]
    events = jv.build_view([execution(records)]).timelines["exec-1"]
    assert [e.data["result"] for e in events] == ["file.txt", "lost output"]
    assert "no matching call" in events[1].data["label"]


def test_md_summary_as_dict_and_double_encoded_ui():
    records = [
        rec("investigation_summary_md", {"content": "# Root cause\n\nDNS"}, 0),
        rec("ui_investigation_summary", {"content": json.dumps(
            {"type": "title", "props": {"level": "abc"}, "text": "Final"})}, 1),
    ]
    view = jv.build_view([execution(records)])
    assert "Root cause" in str(view.summaries[0][1]) and "DNS" in str(view.summaries[0][1])
    assert "Final" in str(view.ui_summary) and "<h3>" in str(view.ui_summary)


def test_malformed_shapes_do_not_crash():
    records = [
        rec("utilization", {"data": "garbage"}, 0),
        rec("utilization", {"data": {"tools": [{"name": "x", "tool_use_count": "7"}], "context_window": []}}, 1),
        rec("finding", {"id": {"weird": 1}, "title": "t"}, 2),
        rec("ui_investigation_summary", {"content": {"type": "title", "props": {"level": {"x": 1}}, "text": "x"}}, 3),
    ]
    view = jv.build_view([execution(records)])
    assert view.usage["tools"][0]["name"] == "x"
    assert view.facts["Findings"][0]["title"] == "t"


def test_latest_record_wins_by_time_not_order():
    later = dict(rec("investigation_summary_md", "new", 0), createdAt=T0.replace(hour=13))
    older = rec("investigation_summary_md", "old", 1)
    view = jv.build_view([execution([later, older])])
    assert "new" in str(view.summaries[0][1])


def test_markdown_pathological_input_is_fast():
    import time
    start = time.monotonic()
    jv.markdown("# a" + " " * 5000 + "x")
    jv.markdown(" *a" * 8000)
    jv.markdown("[a](" * 20000)
    jv.markdown(">" * 3000 + " deep")
    assert time.monotonic() - start < 2
    assert str(jv.markdown("# C#")) == "<h3>C#</h3>"
    assert str(jv.markdown("## Title ##")) == "<h4>Title</h4>"
