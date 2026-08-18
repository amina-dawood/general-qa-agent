from __future__ import annotations

import html
import json
from typing import Any, Dict, Iterable

from .config import Settings, settings


def _escape(value: Any) -> str:
    return html.escape(str(value or ""))


def _list(items: Iterable[Any]) -> str:
    values = [str(item) for item in items if str(item).strip()]
    if not values:
        return "<span class='muted'>None</span>"
    return "<ul>" + "".join(f"<li>{_escape(item)}</li>" for item in values) + "</ul>"


def _seconds(ms: Any) -> str:
    try:
        value = float(ms or 0)
    except (TypeError, ValueError):
        return "0.00s"
    return f"{value / 1000:.2f}s"


class ReportService:
    """Write full JSON evidence plus a compact human-readable HTML report."""

    def __init__(self, config: Settings = settings):
        self.config = config

    def write(self, run: Dict[str, Any]) -> Dict[str, str]:
        # Human-in-the-loop pauses are persisted execution state, not final QA
        # evidence. Do not create partial reports while the tester is acting.
        if run.get("status") == "awaiting_human":
            return {}

        self.config.report_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.config.report_dir / f"{run['id']}.json"
        html_path = self.config.report_dir / f"{run['id']}.html"
        json_path.write_text(
            json.dumps(run, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        html_path.write_text(self._html(run), encoding="utf-8")
        return {"json": str(json_path), "html": str(html_path)}

    def _html(self, run: Dict[str, Any]) -> str:
        result_sections = "".join(self._result_html(result) for result in run.get("results", []))
        usage = run.get("ai_usage") or {}
        run_warnings = run.get("warnings") or []
        warning_html = (
            "<section class='card'><h3>Run notices</h3>" + _list(run_warnings) + "</section>"
            if run_warnings
            else ""
        )
        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(run.get('display_id', 'QA Run'))}</title>
<style>
:root{{--border:#dfe5ee;--soft:#f6f8fb;--ink:#172033;--muted:#667085;--good:#137052;--bad:#a92f43;--warn:#8a5c00;--critical:#a04b00;--info:#315aa6}}
*{{box-sizing:border-box}}body{{font-family:Arial,sans-serif;margin:0;background:#f5f7fb;color:var(--ink);font-size:14px;line-height:1.5}}
main{{max-width:1100px;margin:0 auto;padding:28px}}.card{{background:#fff;border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:14px}}
h1,h2,h3,h4,p{{margin-top:0}}h1{{margin-bottom:4px}}h3{{margin-bottom:8px}}.muted{{color:var(--muted)}}
.metrics{{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px}}.metric{{background:var(--soft);border-radius:8px;padding:10px}}.metric small{{display:block;color:var(--muted)}}
.badge{{display:inline-block;padding:3px 8px;border-radius:999px;background:#edf1f6;font-size:11px;text-transform:uppercase}}.passed,.healthy{{color:var(--good)}}.failed,.error{{color:var(--bad)}}.blocked,.warning{{color:var(--warn)}}.critical{{color:var(--critical)}}.info{{color:var(--info)}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.box{{background:var(--soft);border-radius:8px;padding:10px;overflow-wrap:anywhere}}.box small{{display:block;color:var(--muted);text-transform:uppercase;font-size:10px}}
.performance{{border-left:4px solid #98a2b3}}.performance.warning{{border-left-color:#c48900}}.performance.critical{{border-left-color:#d06400}}.performance.failed{{border-left-color:#b42318}}.performance.healthy{{border-left-color:#16845f}}
.message{{max-width:82%;padding:9px 11px;border-radius:9px;margin:7px 0;background:#eef2f7;white-space:pre-wrap}}.message.assistant{{margin-left:auto;background:#edf5ff}}.message small{{display:block;color:var(--muted);margin-bottom:3px}}
details{{border-top:1px solid var(--border);padding-top:10px;margin-top:12px}}summary{{cursor:pointer;font-weight:bold}}ul{{margin-top:6px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--soft);padding:10px;border-radius:8px}}
@media(max-width:800px){{main{{padding:12px}}.metrics{{grid-template-columns:repeat(2,1fr)}}.grid{{grid-template-columns:1fr}}}}
</style>
</head>
<body><main>
<section class="card">
<h1>{_escape(run.get('display_id', 'QA Run'))}</h1>
<p class="muted">Suite: {_escape(run.get('suite_name', ''))} · {_escape(run.get('started_at', ''))}</p>
<div class="metrics">
<div class="metric"><small>Passed</small><b>{int(run.get('passed_count', 0) or 0)}</b></div>
<div class="metric"><small>Failed</small><b>{int(run.get('failed_count', 0) or 0)}</b></div>
<div class="metric"><small>Blocked</small><b>{int(run.get('blocked_count', 0) or 0)}</b></div>
<div class="metric"><small>Errors</small><b>{int(run.get('error_count', 0) or 0)}</b></div>
<div class="metric"><small>Pass rate</small><b>{float(run.get('pass_rate', 0) or 0):.1f}%</b></div>
<div class="metric"><small>AI tokens</small><b>{int(usage.get('total_tokens', 0) or 0)}</b></div>
</div>
</section>
{warning_html}
{result_sections or '<section class="card"><p class="muted">No test results.</p></section>'}
</main></body></html>"""

    def _result_html(self, result: Dict[str, Any]) -> str:
        outcome = str(result.get("outcome") or "error")
        snapshot = result.get("test_case_snapshot") or {}
        evaluation = result.get("evaluation") or {}
        semantic = evaluation.get("semantic") or {}
        diagnosis = result.get("diagnosis") or {}
        conversation = result.get("conversation") or {}

        turns = "".join(
            f"<div class='message {_escape(turn.get('role'))}'><small>{_escape(turn.get('role', '').upper())}"
            + (f" · {int(turn.get('latency_ms', 0) or 0)} ms" if turn.get("latency_ms") else "")
            + f"</small>{_escape(turn.get('content', ''))}</div>"
            for turn in conversation.get("turns", [])
        )
        rules = evaluation.get("rule_checks") or []
        def rule_label(check: Dict[str, Any]) -> str:
            severity = str(check.get("severity") or "error")
            if severity == "info":
                return "INFO"
            if severity == "warning":
                return "WARN"
            return "PASS" if check.get("passed") else "FAIL"

        rule_html = "".join(
            f"<li class='{_escape(check.get('severity', 'error'))}'><b>{rule_label(check)} - {_escape(check.get('name'))}</b>: {_escape(check.get('message'))}</li>"
            for check in rules
        )
        human_actions = conversation.get("human_actions") or []
        human_html = "".join(
            "<li><b>"
            + _escape((item.get("action") or {}).get("title") or "Human action")
            + "</b> — "
            + _escape(item.get("status") or "")
            + (f" · {_escape(item.get('note'))}" if item.get("note") else "")
            + "</li>"
            for item in human_actions
        )

        return f"""<section class="card">
<h2>{_escape(result.get('test_case_id'))} · {_escape(result.get('title'))}</h2>
<p><span class="badge {outcome}">{_escape(outcome)}</span> &nbsp; Score: {float(result.get('score', 0) or 0):.2f} &nbsp; Priority: {_escape(result.get('priority'))}</p>
<div class="grid">
<div class="box"><small>User goal</small>{_escape(snapshot.get('user_goal'))}</div>
<div class="box"><small>Persona</small>{_escape(snapshot.get('persona'))}</div>
<div class="box"><small>Starting state</small>{_escape(snapshot.get('state_mode'))}</div>
<div class="box"><small>Disclosure</small>{_escape(snapshot.get('disclosure_style', 'progressive'))}</div>
<div class="box"><small>Requirements</small>{_escape(', '.join(result.get('requirement_ids', []) or []))}</div>
<div class="box"><small>Stop reason</small>{_escape(conversation.get('stop_reason'))}</div>
</div>
{self._performance_html(evaluation.get('performance') or {})}
<details><summary>Expected behavior & evaluation</summary>
<p><b>Expected:</b> {_escape(snapshot.get('expected_result'))}</p>
<p><b>Evaluation:</b> {_escape(evaluation.get('summary'))}</p>
{f"<p><b>DeepEval:</b> {float(semantic.get('score', 0) or 0):.2f} / threshold {float(semantic.get('threshold', 0) or 0):.2f}<br>{_escape(semantic.get('reason'))}</p>" if semantic else ''}
{f'<ul>{rule_html}</ul>' if rule_html else ''}
</details>
{self._diagnosis_html(diagnosis) if diagnosis else ''}
{f'<details><summary>Human actions</summary><ul>{human_html}</ul></details>' if human_html else ''}
<details open><summary>Conversation evidence</summary>{turns or '<p class="muted">No conversation turns were recorded.</p>'}</details>
</section>"""

    def _performance_html(self, performance: Dict[str, Any]) -> str:
        if not performance or performance.get("status") == "not_measured":
            return ""
        thresholds = performance.get("thresholds") or {}
        documented_target = int(performance.get("documented_target_ms", 0) or 0)
        documented_text = ""
        if documented_target:
            documented_text = (
                f"<p><b>Documented target:</b> {_seconds(documented_target)} · "
                f"exceeded on {int(performance.get('documented_target_exceeded_count', 0) or 0)} response(s) · "
                + ("enforced" if performance.get("documented_sla_enforced") else "advisory")
                + "</p>"
            )
        return f"""<div class="box performance {_escape(performance.get('status'))}" style="margin-top:12px">
<small>Performance · {_escape(performance.get('status'))}</small>
<p>{_escape(performance.get('message'))}</p>
<div class="grid">
<div><b>Average</b><br>{_seconds(performance.get('average_ms'))}</div>
<div><b>P95</b><br>{_seconds(performance.get('p95_ms'))}</div>
<div><b>Maximum</b><br>{_seconds(performance.get('max_ms'))}</div>
<div><b>Operational bands</b><br>warning &gt; {_seconds(thresholds.get('warning_ms'))}, critical &gt; {_seconds(thresholds.get('critical_ms'))}, fail &gt; {_seconds(thresholds.get('fail_ms'))}</div>
</div>
{documented_text}
</div>"""

    def _diagnosis_html(self, diagnosis: Dict[str, Any]) -> str:
        causes = [
            f"{item.get('cause', '')} ({item.get('confidence', '')})"
            for item in diagnosis.get("likely_causes", []) or []
            if isinstance(item, dict)
        ]
        workflow_html = ""
        if diagnosis.get("workflow_evidence_available") and diagnosis.get("suspected_components"):
            workflow_html = (
                "<h4>Workflow areas to inspect</h4>"
                + _list(diagnosis.get("suspected_components", []) or [])
            )
        return f"""<details><summary>Failure diagnosis</summary>
<p><b>Category:</b> {_escape(diagnosis.get('failure_category'))}</p>
<p>{_escape(diagnosis.get('observed_problem'))}</p>
<h4>Observed evidence</h4>{_list(diagnosis.get('evidence', []) or [])}
<h4>Possible causes (inference)</h4>{_list(causes)}
<h4>Recommended checks</h4>{_list(diagnosis.get('recommended_checks', []) or [])}
{workflow_html}
</details>"""

