#!/usr/bin/env python3
"""
Openclaw error supervisor — Windows host daemon.

Watches .openclaw/cron/runs/*.jsonl for finished cron runs. On error:
  Tier 1: pattern-matched self-repair (restart xhs-bridge for known link
          failures, etc.). Conservative — only acts on patterns that have
          been observed in this repo's runs.
  Tier 2: escalate to Claude Code (running inside the openclaw-gateway
          container) via `docker exec ... claude -p "..." --output-format json`.
          Captures the final report and writes it to
          .openclaw/logs/escalations/.
  Telegram: posts a concise summary to the operator regardless of tier.

Also handles non-error stuck states by watching `consecutiveErrors` in
jobs.json — if a job has >=3 consecutive errors and the last error is an
unfamiliar pattern, escalate even if Tier 1 didn't fire.

State (which runs we've already reacted to) is persisted in
.openclaw/logs/supervisor/state.json so restarts don't re-trigger old errors.

Run with:
  python scripts/error_supervisor.py
or as a Windows scheduled task — see scripts/install_supervisor.ps1.

Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ----------------------- paths & constants ----------------------------------

CONFIG_DIR  = Path(r"./openclaw-runtime\.openclaw")
RUNS_DIR    = CONFIG_DIR / "cron" / "runs"
JOBS_JSON   = CONFIG_DIR / "cron" / "jobs.json"
OPENCLAW_JSON = CONFIG_DIR / "openclaw.json"

LOG_DIR     = CONFIG_DIR / "logs" / "supervisor"
ESC_DIR     = CONFIG_DIR / "logs" / "escalations"
STATE_PATH  = LOG_DIR / "state.json"
SUPER_LOG   = LOG_DIR / "supervisor.log"

# The `openclaw-cli` service is not long-running (entrypoint exits after
# printing help). The gateway container is the always-on one and shares the
# same image, so it has claude installed too.
CLI_CONTAINER = "docker-openclaw-gateway-1"
BRIDGE_CONTAINER = "docker-xhs-bridge-1"
COMPOSE_PROJECT = "docker"  # `docker-` prefix on container names

POLL_INTERVAL_SEC      = 10
ESCALATION_TIMEOUT_SEC = 600          # 10 minutes per claude call
MAX_CLAUDE_TURNS       = 15
MIN_CONSEC_FOR_STUCK   = 3            # for non-error stuck escalations

# ----------------------- tier 1 repair patterns -----------------------------
# Each entry: (regex matched against error string, action callable).
# Conservative: only patterns that have actually been observed in this repo.
# Action returns a short status string for the telegram summary.

def _restart_xhs_bridge() -> str:
    rc = _run(["docker", "restart", BRIDGE_CONTAINER], timeout=30)
    return f"docker restart {BRIDGE_CONTAINER} → exit={rc.returncode}"

def _noop_cookie() -> str:
    return "skipped (cookie expiry handled by xhs-cookie-daily-check job)"

TIER1_RULES: List[Tuple[re.Pattern, Any, str]] = [
    # Bridge / extension link issues — common when watchdog stalls
    (re.compile(r"Bridge\s*✗|扩展\s*✗|bridge.*(down|fail|unreachable)|9333", re.I),
     _restart_xhs_bridge,
     "bridge link issue"),
    # Cookie expired — leave to the dedicated daily-check job, don't double-act
    (re.compile(r"登录\s*✗|cookie\s*(expired|过期|invalid)", re.I),
     _noop_cookie,
     "xhs cookie expired"),
]

# Errors that should NEVER escalate (noise / user action required)
SKIP_ESCALATION = [
    re.compile(r"Delivering to Telegram requires target", re.I),  # config bug, not transient
]

# ----------------------- helpers --------------------------------------------

def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        SUPER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SUPER_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass

def _run(cmd: List[str], timeout: int = 60, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        input=stdin,
    )

def _load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"seen_runs": {}, "first_run_at": time.time()}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _log(f"state.json corrupt — resetting")
        return {"seen_runs": {}, "first_run_at": time.time()}

def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)

def _read_last_finished(jsonl_path: Path) -> Optional[Dict[str, Any]]:
    """Return the last `action:'finished'` event from a run jsonl, or None."""
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("action") == "finished":
            return ev
    return None

def _load_job(job_id: str) -> Optional[Dict[str, Any]]:
    if not JOBS_JSON.exists():
        return None
    try:
        data = json.loads(JOBS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    for job in data.get("jobs", []):
        if job.get("id") == job_id:
            return job
    return None

def _telegram_send(text: str) -> bool:
    try:
        oc = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _log("openclaw.json unreadable — telegram skipped")
        return False
    tg = oc.get("channels", {}).get("telegram", {})
    token = tg.get("botToken")
    if not token:
        _log("no telegram botToken in openclaw.json — telegram skipped")
        return False
    # Pick a default chat id — supervisor is operator-facing, use first
    # configured cron job's delivery target as the recipient.
    chat_id = _first_telegram_chat()
    if not chat_id:
        _log("no chat_id derivable from jobs.json — telegram skipped")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:4000],  # Telegram cap is 4096
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError) as e:
        _log(f"telegram send failed: {e}")
        return False

def _first_telegram_chat() -> Optional[str]:
    if not JOBS_JSON.exists():
        return None
    try:
        data = json.loads(JOBS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    for job in data.get("jobs", []):
        d = job.get("delivery", {})
        if d.get("channel") == "telegram" and d.get("to"):
            return str(d["to"])
    return None

# ----------------------- claude escalation ----------------------------------

ESCALATION_PROMPT_TEMPLATE = """\
You are the Openclaw recovery agent. A scheduled job in this Openclaw deployment
just failed. Diagnose and (where safe) fix it. If the fix would touch shared
state or run anything destructive, write a clear remediation plan in your final
message instead of executing.

## Job
- name: {job_name}
- id: {job_id}
- schedule: {schedule}
- consecutiveErrors: {consec}

## Failure
- error: {error}
- run summary (truncated): {summary}

## Run log (last events)
{run_tail}

## Workspace layout
- Openclaw config: /home/node/.openclaw
- Run logs: /home/node/.openclaw/cron/runs
- Skills: /home/node/.openclaw/skills
- Workspace files: /home/node/.openclaw/workspace

## Tier-1 already attempted
{tier1}

Please:
1. Identify root cause (not just the surface symptom).
2. If a small, safe fix is possible (config tweak, file edit, restarting a
   helper), apply it and verify.
3. If not, output a numbered plan the operator can run.
4. End with a one-paragraph PLAIN-TEXT report titled "## SUMMARY" — what you
   found, what you did, what's left. The supervisor parses this section.
"""

def _build_prompt(job: Dict[str, Any], finished_ev: Dict[str, Any], tier1_note: str, run_tail: str) -> str:
    sched = job.get("schedule", {})
    sched_str = f"{sched.get('kind')}: {sched.get('expr') or sched.get('at') or sched.get('everyMs')}"
    return ESCALATION_PROMPT_TEMPLATE.format(
        job_name=job.get("name", "?"),
        job_id=job.get("id", "?"),
        schedule=sched_str,
        consec=job.get("state", {}).get("consecutiveErrors", "?"),
        error=str(finished_ev.get("error", ""))[:800],
        summary=str(finished_ev.get("summary", ""))[:1500],
        run_tail=run_tail[-2000:],
        tier1=tier1_note,
    )

def _tail_run_log(run_path: Path, n_lines: int = 20) -> str:
    try:
        lines = run_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(could not read run log)"
    return "\n".join(lines[-n_lines:])

def _escalate_to_claude(prompt: str) -> Dict[str, Any]:
    """Run claude inside the gateway container, return parsed result.

    The prompt is piped via stdin to dodge Windows argv quoting edge cases
    (e.g. an arrow `->` substring being interpreted as an unknown CLI option
    when argv crosses Python -> docker -> claude).
    """
    inner = ('claude -p "$(cat)" --output-format json '
             f'--max-turns {MAX_CLAUDE_TURNS} --permission-mode bypassPermissions')
    cmd = ["docker", "exec", "-i", "-w", "/home/node/.openclaw",
           CLI_CONTAINER, "sh", "-c", inner]
    started = time.time()
    try:
        proc = _run(cmd, timeout=ESCALATION_TIMEOUT_SEC, stdin=prompt)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "claude call timed out", "duration_s": ESCALATION_TIMEOUT_SEC}
    duration_s = round(time.time() - started, 1)
    out = (proc.stdout or "").strip()

    parsed: Optional[Dict[str, Any]] = None
    if out:
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = None

    if proc.returncode != 0 and not parsed:
        return {
            "ok": False,
            "error": f"claude exit {proc.returncode}",
            "stderr": (proc.stderr or "")[-1500:],
            "stdout_tail": out[-500:],
            "duration_s": duration_s,
        }

    if not parsed:
        return {
            "ok": True, "report": out[-4000:], "raw": True,
            "duration_s": duration_s, "session_id": "", "turns": 0,
        }

    is_err = bool(parsed.get("is_error"))
    return {
        "ok": (not is_err) and proc.returncode == 0,
        "report": parsed.get("result") or parsed.get("text") or out[-2000:],
        "session_id": parsed.get("session_id", ""),
        "turns": parsed.get("num_turns", 0),
        "duration_s": duration_s,
        "is_error": is_err,
        "subtype": parsed.get("subtype", ""),
        "stderr": (proc.stderr or "")[-500:] if is_err else "",
    }

def _write_escalation_log(job: Dict[str, Any], finished_ev: Dict[str, Any],
                          tier1_note: str, claude_result: Dict[str, Any]) -> Path:
    ESC_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"{ts}-{job.get('id','unknown')[:8]}.md"
    path = ESC_DIR / fname
    body = [
        f"# Escalation report",
        f"- timestamp: {ts}",
        f"- job: {job.get('name','?')} ({job.get('id','?')})",
        f"- error: {finished_ev.get('error','')}",
        f"- tier1: {tier1_note}",
        f"- claude session: {claude_result.get('session_id','-')}",
        f"- turns: {claude_result.get('turns','-')}",
        f"- duration_s: {claude_result.get('duration_s','-')}",
        f"- ok: {claude_result.get('ok')}",
        "",
        "## Report",
        "",
        str(claude_result.get("report") or claude_result.get("error") or "(empty)"),
    ]
    path.write_text("\n".join(body), encoding="utf-8")
    return path

# ----------------------- main loop ------------------------------------------

@dataclass
class HandledRun:
    run_id: str
    handled_at: float = field(default_factory=time.time)
    tier1: str = ""
    escalated: bool = False
    report_path: str = ""

def _scan_once(state: Dict[str, Any]) -> None:
    if not RUNS_DIR.exists():
        return
    seen: Dict[str, Any] = state.setdefault("seen_runs", {})
    first_run_at: float = state.get("first_run_at", time.time())

    for jsonl in sorted(RUNS_DIR.glob("*.jsonl")):
        run_id = jsonl.stem
        if run_id in seen:
            continue
        ev = _read_last_finished(jsonl)
        if ev is None:
            continue  # not finished yet
        # On first launch, mark every already-finished run as seen without
        # acting (avoid spamming on historical errors).
        if jsonl.stat().st_mtime < first_run_at - 60:
            seen[run_id] = {"handled_at": time.time(), "skipped": "pre-startup"}
            continue
        if ev.get("status") != "error":
            seen[run_id] = {"handled_at": time.time(), "ok": True}
            continue

        _handle_error(jsonl, ev, seen)

    _save_state(state)

def _handle_error(run_path: Path, ev: Dict[str, Any], seen: Dict[str, Any]) -> None:
    run_id = run_path.stem
    job_id = ev.get("jobId", "")
    job = _load_job(job_id) or {"id": job_id, "name": "(unknown)", "state": {}}
    err = str(ev.get("error", ""))

    _log(f"error in run {run_id} (job={job.get('name')}): {err[:120]}")

    # Tier 1
    tier1_note = "n/a"
    for pat, action, label in TIER1_RULES:
        if pat.search(err):
            try:
                outcome = action()
                tier1_note = f"{label}: {outcome}"
                _log(f"tier1 fired ({label}): {outcome}")
            except (subprocess.SubprocessError, OSError) as e:
                tier1_note = f"{label}: failed ({e})"
                _log(f"tier1 failed: {e}")
            break

    # Skip rules — known noise we don't want to bother claude with
    if any(p.search(err) for p in SKIP_ESCALATION):
        _log(f"skipping escalation (matches SKIP_ESCALATION): {err[:80]}")
        seen[run_id] = HandledRun(run_id, tier1=tier1_note).__dict__
        _telegram_send(_format_summary(job, ev, tier1_note, claude=None, skipped=True))
        return

    # Tier 2: escalate
    consec = job.get("state", {}).get("consecutiveErrors", 0)
    should_escalate = (consec >= 1) or (tier1_note == "n/a")
    if not should_escalate:
        _log(f"tier1 handled, not escalating (consec={consec})")
        seen[run_id] = HandledRun(run_id, tier1=tier1_note).__dict__
        _telegram_send(_format_summary(job, ev, tier1_note, claude=None))
        return

    prompt = _build_prompt(job, ev, tier1_note, _tail_run_log(run_path))
    _log(f"escalating to claude (job={job.get('name')}, consec={consec})")
    result = _escalate_to_claude(prompt)
    report_path = _write_escalation_log(job, ev, tier1_note, result)
    _log(f"escalation done: ok={result.get('ok')}, report={report_path}")

    seen[run_id] = HandledRun(
        run_id,
        tier1=tier1_note,
        escalated=True,
        report_path=str(report_path),
    ).__dict__
    _telegram_send(_format_summary(job, ev, tier1_note, claude=result, report_path=report_path))

def _format_summary(job: Dict[str, Any], ev: Dict[str, Any], tier1_note: str,
                    claude: Optional[Dict[str, Any]] = None,
                    report_path: Optional[Path] = None,
                    skipped: bool = False) -> str:
    lines = [
        f"⚠️ *Cron error* — `{job.get('name','?')}`",
        f"```",
        f"err: {str(ev.get('error',''))[:160]}",
        f"```",
        f"*Tier 1*: {tier1_note}",
    ]
    if skipped:
        lines.append("*Tier 2*: skipped (known noise)")
    elif claude is None:
        lines.append("*Tier 2*: not invoked")
    elif claude.get("ok"):
        report = (claude.get("report") or "")[:600]
        # Pull SUMMARY section if claude wrote one
        m = re.search(r"## SUMMARY\s*\n+(.+)", report, re.S)
        if m:
            report = m.group(1).strip()[:600]
        lines.append(f"*Tier 2 (claude, {claude.get('turns','?')} turns, "
                     f"{claude.get('duration_s','?')}s)*:")
        lines.append(report)
    else:
        lines.append(f"*Tier 2*: failed — {claude.get('error','')}")
    if report_path:
        lines.append(f"_Full report: {report_path.name}_")
    return "\n".join(lines)

def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ESC_DIR.mkdir(parents=True, exist_ok=True)
    _log("supervisor starting")
    _log(f"  watching: {RUNS_DIR}")
    _log(f"  cli container: {CLI_CONTAINER}")
    _log(f"  poll interval: {POLL_INTERVAL_SEC}s")

    state = _load_state()
    while True:
        try:
            _scan_once(state)
        except (OSError, subprocess.SubprocessError) as e:
            _log(f"scan error: {e}")
        except KeyboardInterrupt:
            _log("supervisor stopping (KeyboardInterrupt)")
            return 0
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    sys.exit(main())
