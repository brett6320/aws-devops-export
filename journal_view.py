"""Turn raw DevOps Agent journal records into a human-readable view model.

Journal record `content` is usually a JSON document serialized as a string.
Known record types observed in the wild:

  message                       chat turn; content blocks: text, thinking,
                                tool_use, tool_result
  utilization                   context-window / tool usage counters
  ui_investigation_summary      progressively updated UI tree (latest wins)
  symptom / finding /
  observation / investigation_gap
                                structured facts with title + description
  investigation_summary(_md)    final summary (JSON + markdown twin)
  mitigation_summary(_md)       mitigation plan (JSON + markdown twin)

Everything here returns markupsafe.Markup built from escaped input, so
untrusted journal text can never inject HTML.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from markupsafe import Markup, escape

FACT_TYPES = {
    "symptom": "Symptoms",
    "finding": "Findings",
    "observation": "Observations",
    "investigation_gap": "Investigation gaps",
}
# Markdown twins are preferred; the JSON versions are only used as fallback.
SUMMARY_TYPES = [
    ("investigation_summary_md", "investigation_summary", "Investigation summary"),
    ("mitigation_summary_md", "mitigation_summary", "Mitigation summary"),
]
FACT_TEXT_KEYS = ("description", "analysis", "summary", "text")
FACT_HIDDEN_KEYS = {"type", "id", "title", *FACT_TEXT_KEYS}
TOOL_RESULT_MAX = 20_000
# Larger inputs are shown verbatim instead of parsed (keeps regex cost bounded).
MARKDOWN_MAX_CHARS = 100_000
MARKDOWN_MAX_LINE = 5_000
MARKDOWN_MAX_DEPTH = 8


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #
def decode_content(content: Any) -> Any:
    """Unwrap JSON-in-a-string (possibly more than once)."""
    for _ in range(3):
        if isinstance(content, str) and content.lstrip()[:1] in ("{", "[", '"'):
            try:
                content = json.loads(content)
                continue
            except ValueError:
                pass
        break
    return content


def _safe_href(url: str) -> str | None:
    url = html.unescape(url).strip()
    return url if urlparse(url).scheme.lower() in ("http", "https") else None


# --------------------------------------------------------------------------- #
# Minimal, escape-first Markdown renderer (headings, lists, tables, fences,
# quotes, hr, bold/italic/code/links). Raw HTML in the source is escaped.
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")


def _inline(text: str) -> str:
    out = []
    for i, part in enumerate(re.split(r"(`[^`\n]+`)", text)):
        if i % 2:
            out.append(f"<code>{escape(part[1:-1])}</code>")
            continue
        s = str(escape(part))
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)

        def link(m: re.Match) -> str:
            href = _safe_href(m.group(2))
            if not href:
                return m.group(0)
            return f'<a href="{escape(href)}" rel="noopener noreferrer">{m.group(1)}</a>'

        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, s)
        out.append(s)
    return "".join(out)


def _is_block_start(line: str) -> bool:
    return bool(_FENCE.match(line) or _HEADING.match(line) or _HR.match(line)
                or _LIST.match(line) or _QUOTE.match(line) or line.lstrip().startswith("|"))


def _render_list(items: list[tuple[int, bool, str]]) -> str:
    out: list[str] = []
    stack: list[tuple[int, str]] = []
    for indent, ordered, text in items:
        tag = "ol" if ordered else "ul"
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if not stack or indent > stack[-1][0]:
            out.append(f"<{tag}><li>")
            stack.append((indent, tag))
        else:
            out.append("</li><li>")
        out.append(_inline(text))
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _heading_text(text: str) -> str:
    # Strip an optional closing "###" (ATX style) without a backtracking regex.
    text = text.rstrip()
    stripped = text.rstrip("#")
    if stripped != text and (not stripped or stripped[-1].isspace()):
        text = stripped.rstrip()
    return text


def markdown(text: Any, _depth: int = 0) -> Markup:
    if not isinstance(text, str):
        return Markup("")
    lines = text.replace("\r\n", "\n").split("\n")
    if (len(text) > MARKDOWN_MAX_CHARS or _depth > MARKDOWN_MAX_DEPTH
            or any(len(line) > MARKDOWN_MAX_LINE for line in lines)):
        return Markup(f"<pre>{escape(text)}</pre>")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif _FENCE.match(line):
            marker = _FENCE.match(line).group(1)
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith(marker):
                code.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre><code>{escape(chr(10).join(code))}</code></pre>")
        elif _HEADING.match(line):
            m = _HEADING.match(line)
            level = min(len(m.group(1)) + 2, 6)  # page already uses h1/h2
            out.append(f"<h{level}>{_inline(_heading_text(m.group(2)))}</h{level}>")
            i += 1
        elif _HR.match(line):
            out.append("<hr>")
            i += 1
        elif line.lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            head = _split_row(line)
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in head)
            tb = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table></div>')
        elif _LIST.match(line):
            items: list[tuple[int, bool, str]] = []
            while i < len(lines) and lines[i].strip():
                m = _LIST.match(lines[i])
                if m:
                    items.append((len(m.group(1).expandtabs(4)), m.group(2)[0].isdigit(), m.group(3)))
                elif items and lines[i].startswith((" ", "\t")):
                    indent, ordered, prev = items[-1]
                    items[-1] = (indent, ordered, prev + " " + lines[i].strip())
                else:
                    break
                i += 1
            out.append(_render_list(items))
        elif _QUOTE.match(line):
            quoted = []
            while i < len(lines) and _QUOTE.match(lines[i]):
                quoted.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            out.append(f"<blockquote>{markdown(chr(10).join(quoted), _depth + 1)}</blockquote>")
        else:
            para = [line.strip()]
            i += 1
            while i < len(lines) and lines[i].strip() and not _is_block_start(lines[i]):
                para.append(lines[i].strip())
                i += 1
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
    return Markup("".join(out))


# --------------------------------------------------------------------------- #
# Generic value rendering (fallback for unknown structures)
# --------------------------------------------------------------------------- #
def _label(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def render_value(value: Any, depth: int = 0) -> Markup:
    """Render arbitrary JSON as nested definition lists instead of raw JSON."""
    if value is None or value == "" or value == [] or value == {}:
        return Markup('<span class="muted">—</span>')
    if isinstance(value, bool):
        return Markup("yes" if value else "no")
    if isinstance(value, (int, float)):
        return escape(str(value))
    if isinstance(value, str):
        decoded = decode_content(value)
        if decoded is not value:
            return render_value(decoded, depth)
        if "\n" in value or len(value) > 200:
            return Markup(f'<div class="md">{markdown(value)}</div>')
        return escape(value)
    if depth > 6:
        return Markup(f"<pre>{escape(json.dumps(value, indent=2, default=str))}</pre>")
    if isinstance(value, list):
        if all(not isinstance(v, (dict, list)) for v in value):
            return Markup(", ".join(str(render_value(v, depth + 1)) for v in value))
        return Markup("<ol class=\"kv-list\">" + "".join(
            f"<li>{render_value(v, depth + 1)}</li>" for v in value) + "</ol>")
    if isinstance(value, dict):
        rows = "".join(
            f"<dt>{escape(_label(str(k)))}</dt><dd>{render_value(v, depth + 1)}</dd>"
            for k, v in value.items() if v not in (None, "", [], {})
        )
        return Markup(f'<dl class="kv">{rows}</dl>') if rows else Markup('<span class="muted">—</span>')
    return escape(str(value))


# --------------------------------------------------------------------------- #
# UI summary tree (ui_investigation_summary)
# --------------------------------------------------------------------------- #
def render_ui(node: Any) -> Markup:
    if not isinstance(node, dict):
        return Markup("")
    kind = node.get("type")
    props = node.get("props") if isinstance(node.get("props"), dict) else {}
    text = node.get("text")
    kids = "".join(str(render_ui(c)) for c in node.get("children") or [])
    t = _inline(text) if isinstance(text, str) else ""
    if kind == "title":
        try:
            level = int(props.get("level", 1))
        except (TypeError, ValueError):
            level = 1
        level = max(3, min(level + 2, 6))
        return Markup(f"<h{level}>{t}</h{level}>{kids}")
    if kind == "text":
        return Markup(f"<p>{t}</p>{kids}")
    if kind == "markdown":
        return Markup(f'<div class="md">{markdown(text)}</div>{kids}')
    if kind == "badge":
        return Markup(f'<span class="badge">{t}</span>{kids}')
    if kind == "separator":
        return Markup("<hr>")
    if kind == "card":
        return Markup(f'<div class="card">{t}{kids}</div>')
    if kind == "card-title":
        return Markup(f"<h4>{t}</h4>{kids}")
    if kind == "card-description":
        return Markup(f'<p class="muted">{t}</p>{kids}')
    if kind == "accordion-item":
        return Markup(f"<details>{kids}</details>")
    if kind == "accordion-trigger":
        return Markup(f"<summary>{t}{kids}</summary>")
    # container, card-header, card-content, accordion, accordion-content, unknown
    return Markup(f"<div>{t and f'<p>{t}</p>'}{kids}</div>")


# --------------------------------------------------------------------------- #
# View model
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    kind: str  # prompt | assistant | thinking | tool | other
    created_at: Any
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskView:
    summaries: list[tuple[str, Markup]] = field(default_factory=list)
    ui_summary: Markup | None = None
    facts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    timelines: dict[str, list[Event]] = field(default_factory=dict)  # execution_id -> events
    usage: dict[str, Any] | None = None
    record_types: dict[str, int] = field(default_factory=dict)


def _tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict):
            if isinstance(c.get("text"), str):
                parts.append(c["text"])
            elif "json" in c:
                parts.append(json.dumps(c["json"], indent=2, default=str))
            else:
                parts.append(json.dumps(c, indent=2, default=str))
        else:
            parts.append(str(c))
    return "\n\n".join(parts)


def _pretty_result(text: str) -> tuple[str, bool]:
    decoded = decode_content(text)
    if decoded is not text:
        text = json.dumps(decoded, indent=2, ensure_ascii=False, default=str)
    if len(text) > TOOL_RESULT_MAX:
        return text[:TOOL_RESULT_MAX], True
    return text, False


LABEL_KEYS = ("file_path", "path", "expression", "resource_identifier", "attribute_value",
              "name", "memory_store_id")


def _tool_call_label(name: str, tool_input: Any) -> str:
    """One-line summary of a tool call, e.g. 'use_aws: sso-admin ListInstances · us-east-1'."""
    if not isinstance(tool_input, dict) or not tool_input:
        return name
    if tool_input.get("service_name") and tool_input.get("operation_name"):
        label = f"{name}: {tool_input['service_name']} {tool_input['operation_name']}"
    else:
        label = name
        for key in LABEL_KEYS:
            value = tool_input.get(key)
            if isinstance(value, (str, int, float)) and 0 < len(str(value)) <= 120 and "\n" not in str(value):
                label = f"{name}({value})"
                break
    region = tool_input.get("aws_region") or tool_input.get("region")
    if isinstance(region, str) and region:
        label += f" · {region}"
    return label


def _block_id(block: dict[str, Any]) -> str | None:
    value = block.get("tool_use_id") or block.get("toolUseId") or block.get("id")
    return str(value) if value is not None else None


def _message_events(record: dict[str, Any], msg: dict[str, Any], results: dict[str, dict],
                    paired: set[str]) -> list[Event]:
    events = []
    role = msg.get("role")
    ts = record.get("createdAt")
    for block in msg.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type") or ("text" if "text" in block else None)
        if btype == "text" and isinstance(block.get("text"), str) and block["text"].strip():
            kind = "assistant" if role == "assistant" else "prompt"
            decoded = decode_content(block["text"])
            body = markdown(decoded) if isinstance(decoded, str) else render_value(decoded)
            events.append(Event(kind, ts, {"html": body}))
        elif btype == "thinking" and block.get("thinking"):
            thinking = block["thinking"]
            body = markdown(thinking) if isinstance(thinking, str) else render_value(thinking)
            events.append(Event("thinking", ts, {"html": body}))
        elif btype == "tool_use":
            result = results.pop(_block_id(block), None) or {}
            text, truncated = _pretty_result(_tool_result_text(result)) if result else ("", False)
            tool_input = block.get("input")
            events.append(Event("tool", ts, {
                "name": block.get("tool_name") or block.get("name") or "tool",
                "label": _tool_call_label(block.get("tool_name") or block.get("name") or "tool", tool_input),
                "input": render_value(tool_input) if tool_input else None,
                "status": result.get("status", "no result" if not result else ""),
                "result": text,
                "truncated": truncated,
            }))
        elif btype == "tool_result":
            if _block_id(block) in paired:
                continue  # shown together with its tool_use
            text, truncated = _pretty_result(_tool_result_text(block))
            events.append(Event("tool", ts, {
                "name": "tool result", "label": "tool result (no matching call)", "input": None,
                "status": block.get("status", ""), "result": text, "truncated": truncated,
            }))
        else:
            events.append(Event("other", ts, {"type": btype or "block", "html": render_value(block)}))
    return events


LATEST_TYPES = {"utilization", "ui_investigation_summary", *(t for pair in SUMMARY_TYPES for t in pair[:2])}


def _ts_key(record: dict[str, Any]) -> float:
    ts = record.get("createdAt")
    return ts.timestamp() if hasattr(ts, "timestamp") else 0.0


def _keep_latest(latest: dict[str, Any], rtype: str, record: dict[str, Any], value: Any) -> None:
    # Executions are visited in tree order, so compare timestamps, not position.
    key = _ts_key(record)
    if rtype not in latest or key >= latest[rtype][0]:
        latest[rtype] = (key, value)


def _md_body(value: Any) -> Markup:
    if isinstance(value, str):
        return markdown(value)
    if isinstance(value, dict):
        for k in ("content", "markdown", "text", "summary"):
            if isinstance(value.get(k), str):
                return markdown(value[k])
    return render_value(value)


def build_view(executions: list[Any]) -> TaskView:
    """Build a TaskView from Execution objects (with .records lists)."""
    view = TaskView()
    latest: dict[str, Any] = {}
    facts: dict[str, dict[str, dict[str, Any]]] = {t: {} for t in FACT_TYPES}

    for ex in executions:
        decoded = [(r, decode_content(r.get("content"))) for r in ex.records]
        results: dict[str, dict] = {}
        call_ids: set[str] = set()
        for _, c in decoded:
            if isinstance(c, dict) and isinstance(c.get("content"), list):
                for b in c["content"]:
                    if isinstance(b, dict) and _block_id(b):
                        if b.get("type") == "tool_result":
                            results[_block_id(b)] = b
                        elif b.get("type") == "tool_use":
                            call_ids.add(_block_id(b))
        paired = call_ids & set(results)

        events: list[Event] = []
        for record, c in decoded:
            rtype = record.get("recordType", "")
            view.record_types[rtype] = view.record_types.get(rtype, 0) + 1
            if rtype == "message" and isinstance(c, dict):
                events.extend(_message_events(record, c, results, paired))
            elif rtype in LATEST_TYPES:
                _keep_latest(latest, rtype, record, c)
            elif rtype in FACT_TYPES and isinstance(c, dict):
                key = str(c.get("id") or record.get("recordId"))
                prev = facts[rtype].get(key)
                if prev is None or _ts_key(record) >= prev[0]:
                    facts[rtype][key] = (_ts_key(record), c)
            else:
                events.append(Event("other", record.get("createdAt"), {
                    "type": rtype,
                    "html": markdown(c) if isinstance(c, str) else render_value(c),
                }))
        view.timelines[ex.execution_id] = events

    latest = {k: v for k, (_, v) in latest.items()}
    for md_type, json_type, title in SUMMARY_TYPES:
        if md_type in latest:
            view.summaries.append((title, _md_body(latest[md_type])))
        elif json_type in latest:
            body = latest[json_type]
            if isinstance(body, dict):
                body = {k: v for k, v in body.items() if k != "type"}
            view.summaries.append((title, render_value(body)))

    if "ui_investigation_summary" in latest:
        ui = latest["ui_investigation_summary"]
        tree = decode_content(ui.get("content", ui)) if isinstance(ui, dict) else ui
        rendered = render_ui(tree) if isinstance(tree, dict) else Markup("")
        view.ui_summary = rendered if str(rendered).strip() else render_value(tree)

    for rtype, label in FACT_TYPES.items():
        items = []
        for _, c in facts[rtype].values():
            text = next((c[k] for k in FACT_TEXT_KEYS if isinstance(c.get(k), str) and c[k].strip()), None)
            extra = {k: v for k, v in c.items() if k not in FACT_HIDDEN_KEYS and v not in (None, "", [], {})}
            items.append({
                "title": c.get("title") or c.get("id") or rtype,
                "html": markdown(text) if text else Markup(""),
                "extra": render_value(extra) if extra else None,
            })
        if items:
            view.facts[label] = items

    util = latest.get("utilization")
    data = util.get("data", util) if isinstance(util, dict) else None
    if isinstance(data, dict):
        def count(t: dict[str, Any]) -> float:
            n = t.get("tool_use_count")
            return n if isinstance(n, (int, float)) else 0

        window = data.get("context_window") if isinstance(data.get("context_window"), dict) else {}
        tools = data.get("tools") if isinstance(data.get("tools"), list) else []
        view.usage = {
            "context": window.get("utilization"),
            "compactions": window.get("compaction_count"),
            "tools": sorted((t for t in tools if isinstance(t, dict)), key=lambda t: -count(t)),
        }
    return view
