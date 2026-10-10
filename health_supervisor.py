#!/usr/bin/env python3
"""
Openclaw periodic health supervisor — runs once per hour via schtasks.

Companion to error_supervisor.py. Where that script reacts to *new* error
events as they're written, this one does an hourly sweep over the *standing
state* of the deployment and catches problems the event-driven supervisor
can't see:

  - containers Up + healthy?
  - gateway:18789 / xhs-bridge:9333 actually responding?
  - claude in container still authenticated?
  - jobs.json: any enabled job with consecutiveErrors >= 2?
  - past hour's run logs: any errors not already handled by event supervisor?
  - jobs that *were* escalated but are still failing (escalation didn't take)?

Findings are bucketed:

  tier1 (auto-fix, no claude):
    - container not Up           -> docker compose up -d <service>
    - container Restarting       -> docker restart <name>
    - port not responding but
      container Up                -> docker restart <name>

  tier2 (escalate to claude):
    - cron jobs with consecutiveErrors >= 2
    - jobs that were escalated but error returned

  block (operator action required):
    - claude not authenticated   -> ask operator to re-run init_claude_state.ps1

If anything was found, a single batched claude call diagnoses all the cron
problems together (cheaper than one call per job, and lets claude see the
whole picture). A Telegram summary is sent only when something interesting
happened — no findings == no notification.

State is shared with error_supervisor.py via state.json:

  {
    "seen_runs":  { "<runId>": {...} },         # written by event supervisor
    "job_health": { "<jobId>": {...} },         # written by this script
    "last_health_sweep_at": <unix_ts>,
    "first_run_at": <unix_ts>
  }

Stdlib only. Run with `python scripts/health_supervisor.py`.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ----------------------- shared paths (mirror error_supervisor.py) ----------

CONFIG_DIR    = Path(r"./openclaw-runtime\.openclaw")
RUNS_DIR      = CONFIG_DIR / "cron" / "runs"
JOBS_JSON     = CONFIG_DIR / "cron" / "jobs.json"
OPENCLAW_JSON = CONFIG_DIR / "openclaw.json"

LOG_DIR       = CONFIG_DIR / "logs" / "supervisor"
ESC_DIR       = CONFIG_DIR / "logs" / "escalations"
STATE_PATH    = LOG_DIR / "state.json"
HEALTH_LOG    = LOG_DIR / "health.log"

GATEWAY_CONTAINER = "docker-openclaw-gateway-1"
BRIDGE_CONTAINER  = "docker-xhs-bridge-1"
COMPOSE_FILE      = r"./openclaw-runtime\docker\docker-compose.yml"

GATEWAY_HEALTH_URL = "http://127.0.0.1:18789/healthz"
BRIDGE_HEALTH_URL  = "http://127.0.0.1:9333/health"

ESCALATION_TIMEOUT_SEC = 600
MAX_CLAUDE_TURNS       = 15  # diagnosis takes 5-12 turns typically
CONSEC_ERR_THRESHOLD   = 2
LOOKBACK_HOURS         = 1   # how far back to scan run logs

# ----------------------- helpers --------------------------------------------

def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        HEALTH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with HEALTH_LOG.open("a", encoding="utf-8") as f:
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
        return {"seen_runs": {}, "job_health": {}, "first_run_at": time.time()}
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _log("state.json corrupt — starting fresh")
        return {"seen_runs": {}, "job_health": {}, "first_run_at": time.time()}
    s.setdefault("seen_runs", {})
    s.setdefault("job_health", {})
    return s

def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["last_health_sweep_at"] = time.time()
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)

def _http_ok(url: str, timeout: float = 5.0) -> Tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(512).decode("utf-8", errors="replace")
            return resp.status == 200, body[:200]
    except (urllib.error.URLError, TimeoutError, ConnectionResetError, socket.timeout) as e:
        return False, str(e)

def _read_last_finished(jsonl_path: Path) -> Optional[Dict[str, Any]]:
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

def _telegram_send(text: str) -> bool:
    try:
        oc = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    tg = oc.get("channels", {}).get("telegram", {})
    token = tg.get("botToken")
    if not token:
        return False
    chat_id = _first_telegram_chat()
    if not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError):
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

# ----------------------- the 6 health checks --------------------------------

def check_containers() -> List[Dict[str, Any]]:
    """Returns finding list. Each finding has fields: kind, severity, msg, fix?."""
    findings: List[Dict[str, Any]] = []
    try:
        # `--all` so we see stopped/exited containers too — without it, a
        # service whose container has crashed appears to "not exist" and we'd
        # silently skip it.
        proc = _run(["docker", "compose", "-f", COMPOSE_FILE, "ps", "--all",
                     "--format", "json"], timeout=20)
    except subprocess.SubprocessError as e:
        findings.append({"kind": "compose_ps_failed", "severity": "block",
                         "msg": f"docker compose ps failed: {e}"})
        return findings
    if proc.returncode != 0:
        findings.append({"kind": "compose_ps_failed", "severity": "block",
                         "msg": f"compose ps exit {proc.returncode}: {proc.stderr[:200]}"})
        return findings

    # docker compose ps --format json emits one JSON object per line OR a JSON array,
    # depending on version. Handle both.
    text = proc.stdout.strip()
    services: List[Dict[str, Any]] = []
    if text.startswith("["):
        try:
            services = json.loads(text)
        except json.JSONDecodeError:
            pass
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                services.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Services that are designed to run once and exit (one-shot CLI entrypoints
    # without a restart policy). docker compose ps shows them as "exited" but
    # that's expected, not a fault.
    ONESHOT_SERVICES = {"openclaw-cli"}

    for svc in services:
        name      = svc.get("Name", svc.get("name", "?"))
        svc_name  = svc.get("Service", svc.get("service", ""))
        state     = svc.get("State", svc.get("state", ""))
        health    = svc.get("Health", svc.get("health", ""))
        status    = svc.get("Status", svc.get("status", ""))
        if svc_name in ONESHOT_SERVICES:
            continue  # exited == expected for one-shot services
        if state in ("running",) and health in ("healthy", "", None) and "Restarting" not in status:
            continue
        # Anomaly
        sev = "tier1"
        fix = {"action": "restart", "container": name}
        if state == "exited":
            fix = {"action": "compose_up", "service": svc.get("Service", svc.get("service", ""))}
        findings.append({
            "kind": "container_unhealthy", "severity": sev,
            "msg": f"{name}: state={state} status={status} health={health}",
            "fix": fix,
        })
    return findings

def check_endpoints() -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for url, ctr in [(GATEWAY_HEALTH_URL, GATEWAY_CONTAINER),
                     (BRIDGE_HEALTH_URL,  BRIDGE_CONTAINER)]:
        ok, body = _http_ok(url)
        if not ok:
            findings.append({
                "kind": "endpoint_down", "severity": "tier1",
                "msg": f"{url} unreachable: {body[:120]}",
                "fix": {"action": "restart", "container": ctr},
            })
    return findings

def check_claude_auth() -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    try:
        ver = _run(["docker", "exec", GATEWAY_CONTAINER, "claude", "--version"], timeout=15)
    except subprocess.SubprocessError as e:
        findings.append({"kind": "claude_unavailable", "severity": "block",
                         "msg": f"docker exec claude --version failed: {e}"})
        return findings
    if ver.returncode != 0:
        findings.append({"kind": "claude_unavailable", "severity": "block",
                         "msg": f"claude --version exit {ver.returncode}: {ver.stderr[:120]}"})
        return findings

    # Cheapest possible auth probe: 1 turn, no thinking, ask for a literal.
    try:
        probe = _run([
            "docker", "exec", GATEWAY_CONTAINER,
            "claude", "-p", "reply with the single word: ok",
            "--output-format", "json", "--max-turns", "1",
        ], timeout=60)
    except subprocess.TimeoutExpired:
        findings.append({"kind": "claude_probe_timeout", "severity": "tier2",
                         "msg": "claude probe timed out (token may be slow but not necessarily dead)"})
        return findings
    if probe.returncode != 0:
        findings.append({"kind": "claude_auth_failed", "severity": "block",
                         "msg": f"claude probe exit {probe.returncode}: {probe.stderr[:200]}",
                         "hint": "re-run scripts/init_claude_state.ps1 to refresh OAuth token"})
        return findings
    try:
        pj = json.loads(probe.stdout)
        if pj.get("is_error"):
            findings.append({"kind": "claude_auth_failed", "severity": "block",
                             "msg": f"claude returned is_error: {str(pj)[:200]}",
                             "hint": "re-run scripts/init_claude_state.ps1"})
    except json.JSONDecodeError:
        findings.append({"kind": "claude_probe_unparseable", "severity": "tier2",
                         "msg": f"claude probe stdout not JSON: {probe.stdout[:200]}"})
    return findings

def check_cron_jobs() -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if not JOBS_JSON.exists():
        return findings
    try:
        data = json.loads(JOBS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        findings.append({"kind": "jobs_json_unreadable", "severity": "block",
                         "msg": f"jobs.json: {e}"})
        return findings
    for job in data.get("jobs", []):
        if not job.get("enabled"):
            continue
        st = job.get("state", {})
        consec = int(st.get("consecutiveErrors", 0))
        if consec >= CONSEC_ERR_THRESHOLD and st.get("lastRunStatus") == "error":
            findings.append({
                "kind": "cron_persistent_failure", "severity": "tier2",
                "msg": (f"job '{job.get('name')}' has consecutiveErrors={consec}, "
                        f"lastError={str(st.get('lastError',''))[:120]}"),
                "job_id": job.get("id"),
                "job_name": job.get("name"),
                "consec": consec,
                "last_error": str(st.get("lastError", ""))[:300],
            })
    return findings

def check_recent_runs(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Errors in the last LOOKBACK_HOURS that the event supervisor didn't handle."""
    findings: List[Dict[str, Any]] = []
    if not RUNS_DIR.exists():
        return findings
    cutoff = time.time() - LOOKBACK_HOURS * 3600
    seen   = state.get("seen_runs", {})
    for jsonl in sorted(RUNS_DIR.glob("*.jsonl")):
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        ev = _read_last_finished(jsonl)
        if not ev or ev.get("status") != "error":
            continue
        run_id = jsonl.stem
        prev = seen.get(run_id)
        if prev and prev.get("escalated"):
            # Event supervisor handled it. But did the same job recover after?
            # If jobs.json still shows error for this job, mark as "escalation
            # didn't stick" so claude gets a second look with that context.
            findings.append({
                "kind": "escalation_no_recovery_candidate",
                "severity": "info",
                "msg": f"run {run_id[:8]} was escalated; checking job recovery in cron findings",
                "run_id": run_id,
                "job_id": ev.get("jobId"),
            })
            continue
        # Either event supervisor isn't running, or this slipped through.
        findings.append({
            "kind": "unhandled_error_run",
            "severity": "tier2",
            "msg": (f"run {run_id[:8]} (job={ev.get('jobId','?')[:8]}) errored "
                    f"and was not handled by event supervisor"),
            "run_id": run_id,
            "job_id": ev.get("jobId"),
            "error": str(ev.get("error", ""))[:300],
        })
    return findings

def check_escalation_no_recovery(state: Dict[str, Any], cron_findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Jobs that were escalated but are still in error state == fix didn't stick."""
    findings: List[Dict[str, Any]] = []
    seen = state.get("seen_runs", {})
    failing_job_ids = {f.get("job_id") for f in cron_findings if f.get("kind") == "cron_persistent_failure"}
    if not failing_job_ids:
        return findings
    # For each currently failing job, find the most recent escalated run.
    for run_id, info in seen.items():
        if not info.get("escalated"):
            continue
        # We don't store job_id in seen directly; cross-check via run file.
        run_path = RUNS_DIR / f"{run_id}.jsonl"
        if not run_path.exists():
            continue
        ev = _read_last_finished(run_path)
        if not ev:
            continue
        jid = ev.get("jobId")
        if jid in failing_job_ids:
            findings.append({
                "kind": "escalation_did_not_stick",
                "severity": "tier2",
                "msg": f"job {jid[:8]} was escalated (run {run_id[:8]}) but is still failing",
                "job_id": jid,
                "previous_report": info.get("report_path", ""),
            })
    return findings

# ----------------------- tier 1 actions -------------------------------------

def apply_tier1(findings: List[Dict[str, Any]]) -> List[str]:
    actions_taken: List[str] = []
    seen_fixes = set()
    for f in findings:
        if f.get("severity") != "tier1":
            continue
        fix = f.get("fix", {})
        action = fix.get("action")
        target = fix.get("container") or fix.get("service") or ""
        key = (action, target)
        if key in seen_fixes:
            continue  # don't restart same container twice in one sweep
        seen_fixes.add(key)
        try:
            if action == "restart" and target:
                r = _run(["docker", "restart", target], timeout=30)
                actions_taken.append(f"restart {target} -> exit {r.returncode}")
            elif action == "compose_up" and target:
                r = _run(["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", target], timeout=60)
                actions_taken.append(f"compose up -d {target} -> exit {r.returncode}")
        except subprocess.SubprocessError as e:
            actions_taken.append(f"{action} {target} FAILED: {e}")
    return actions_taken

# ----------------------- tier 2: batched claude escalation ------------------

ESC_PROMPT = """\
Openclaw health supervisor — DIAGNOSIS ONLY (no edits/writes/restarts).
You're at /home/node/.openclaw. ./cron/jobs.json + ./cron/runs/*.jsonl are
your main inputs.

## Standing issues
{issues}

## Already done this sweep
{tier1_actions}

## Prior escalation outcomes (don't repeat these)
{prior_escalations}

Pick the SINGLE most likely root cause among the issues above. Investigate
just enough to confirm it (3-6 read-only steps). If multiple issues share
a cause, name it once. Aim to finish in under 12 turns.

End your final message with exactly:

## SUMMARY
<one paragraph: root cause + the single most important next action for the
host operator. Plain text. No bullets. No code fences.>
"""

def _build_batch_prompt(findings: List[Dict[str, Any]], tier1_done: List[str], state: Dict[str, Any]) -> str:
    issues_lines = []
    for i, f in enumerate(findings, 1):
        if f.get("severity") not in ("tier2", "block"):
            continue
        issues_lines.append(f"{i}. [{f.get('kind')}] {f.get('msg')}")
        if f.get("last_error"):
            issues_lines.append(f"   last_error: {f['last_error'][:200]}")
    if not issues_lines:
        issues_lines.append("(none — only tier1 issues this sweep)")

    prior_lines = []
    seen = state.get("seen_runs", {})
    for f in findings:
        if f.get("kind") != "escalation_did_not_stick":
            continue
        rp = f.get("previous_report")
        if rp and Path(rp).exists():
            try:
                txt = Path(rp).read_text(encoding="utf-8", errors="replace")
                # Just the SUMMARY section if there is one
                m = re.search(r"## SUMMARY\s*\n(.+)", txt, re.S)
                snippet = m.group(1).strip()[:600] if m else txt[-600:]
                prior_lines.append(f"- job {f.get('job_id','?')[:8]}: {snippet}")
            except OSError:
                pass
    if not prior_lines:
        prior_lines.append("(no prior escalations)")

    return ESC_PROMPT.format(
        issues="\n".join(issues_lines),
        tier1_actions="\n".join(f"- {a}" for a in tier1_done) or "(none)",
        prior_escalations="\n".join(prior_lines),
    )

def _escalate_batched(prompt: str) -> Dict[str, Any]:
    # Pass prompt via stdin to avoid Windows argv quoting edge cases (e.g. an
    # arrow `->` substring being interpreted as an unknown CLI option). The
    # `sh -c 'claude -p "$(cat)" ...'` pattern means claude's argv is fixed
    # and the prompt body flows in safely as a single string.
    # Diagnosis-only call. In headless `-p` mode, `default` + `acceptEdits`
    # both still prompt for non-edit tool use (Bash etc.), which can't be
    # answered without a TTY → tools get denied and claude burns turns.
    # `bypassPermissions` is the documented headless mode. The prompt itself
    # constrains claude to read-only investigation; safety lives in the
    # prompt, not the permission system.
    inner = ('claude -p "$(cat)" --output-format json '
             f'--max-turns {MAX_CLAUDE_TURNS} --permission-mode bypassPermissions')
    cmd = ["docker", "exec", "-i", "-w", "/home/node/.openclaw",
           GATEWAY_CONTAINER, "sh", "-c", inner]
    started = time.time()
    try:
        proc = _run(cmd, timeout=ESCALATION_TIMEOUT_SEC, stdin=prompt)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "claude call timed out", "duration_s": ESCALATION_TIMEOUT_SEC}
    duration_s = round(time.time() - started, 1)
    out = (proc.stdout or "").strip()

    # claude exits 1 when is_error=true (including max-turns / permission denial).
    # In those cases stdout still contains the JSON result envelope.
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

def _write_health_report(findings: List[Dict[str, Any]], tier1_actions: List[str],
                         claude_result: Optional[Dict[str, Any]]) -> Path:
    ESC_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ESC_DIR / f"{ts}-health-sweep.md"
    body = [f"# Hourly health sweep — {ts}", ""]
    body.append("## Findings")
    for f in findings:
        body.append(f"- [{f.get('severity')}/{f.get('kind')}] {f.get('msg')}")
    body.append("")
    body.append("## Tier 1 actions")
    body.extend(f"- {a}" for a in tier1_actions) if tier1_actions else body.append("- (none)")
    body.append("")
    if claude_result:
        body.append(f"## Claude escalation")
        body.append(f"- ok: {claude_result.get('ok')}")
        body.append(f"- is_error: {claude_result.get('is_error', '-')}")
        body.append(f"- subtype: {claude_result.get('subtype', '-')}")
        body.append(f"- session: {claude_result.get('session_id','-')}")
        body.append(f"- turns: {claude_result.get('turns','-')}")
        body.append(f"- duration_s: {claude_result.get('duration_s','-')}")
        body.append("")
        if claude_result.get("stderr"):
            body.append("### stderr (tail)")
            body.append("```")
            body.append(str(claude_result.get("stderr")))
            body.append("```")
            body.append("")
        body.append("### Report")
        body.append(str(claude_result.get("report") or claude_result.get("error") or "(empty)"))
    path.write_text("\n".join(body), encoding="utf-8")
    return path

# ----------------------- main sweep -----------------------------------------

def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log("hourly health sweep starting")
    state = _load_state()

    all_findings: List[Dict[str, Any]] = []
    all_findings += check_containers()
    all_findings += check_endpoints()
    all_findings += check_claude_auth()
    cron_findings = check_cron_jobs()
    all_findings += cron_findings
    all_findings += check_recent_runs(state)
    all_findings += check_escalation_no_recovery(state, cron_findings)

    # filter informational entries from the headline list (kept in report)
    actionable = [f for f in all_findings if f.get("severity") in ("tier1", "tier2", "block")]

    _log(f"findings: {len(all_findings)} total, {len(actionable)} actionable")
    for f in all_findings:
        _log(f"  [{f.get('severity')}/{f.get('kind')}] {f.get('msg')}")

    if not actionable:
        _log("all clean — no telegram, no escalation")
        # update job_health snapshot
        _record_job_health(state, all_findings)
        _save_state(state)
        return 0

    # Tier 1 first
    tier1_done = apply_tier1(all_findings)
    for a in tier1_done:
        _log(f"tier1: {a}")

    # Re-run endpoint check after tier1 to see if it cleared
    if tier1_done:
        time.sleep(5)
        post_endpoints = check_endpoints()
        # If endpoints recovered, drop those findings
        recovered = [f for f in all_findings if f.get("kind") == "endpoint_down"
                     and not any(p["msg"] == f["msg"] for p in post_endpoints)]
        if recovered:
            _log(f"tier1 recovered {len(recovered)} endpoint(s)")
            actionable = [f for f in actionable if f not in recovered]

    # Tier 2: claude (batched)
    tier2_findings = [f for f in actionable if f.get("severity") == "tier2"]
    block_findings = [f for f in actionable if f.get("severity") == "block"]
    claude_result: Optional[Dict[str, Any]] = None
    if tier2_findings or block_findings:
        prompt = _build_batch_prompt(actionable, tier1_done, state)
        _log(f"escalating to claude ({len(tier2_findings)} tier2, {len(block_findings)} block)")
        claude_result = _escalate_batched(prompt)
        _log(f"escalation: ok={claude_result.get('ok')} turns={claude_result.get('turns','-')} "
             f"dur={claude_result.get('duration_s','-')}s")

    report_path = _write_health_report(all_findings, tier1_done, claude_result)
    _log(f"report written: {report_path}")

    # Telegram summary
    msg = _format_telegram(all_findings, tier1_done, claude_result, report_path)
    _telegram_send(msg)

    _record_job_health(state, all_findings)
    _save_state(state)
    return 0

def _record_job_health(state: Dict[str, Any], findings: List[Dict[str, Any]]) -> None:
    jh = state.setdefault("job_health", {})
    failing = {f.get("job_id"): f for f in findings if f.get("kind") == "cron_persistent_failure"}
    # mark all jobs we touched
    if not JOBS_JSON.exists():
        return
    try:
        data = json.loads(JOBS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    now = time.time()
    for job in data.get("jobs", []):
        if not job.get("enabled"):
            continue
        jid = job.get("id")
        st = job.get("state", {})
        jh[jid] = {
            "name": job.get("name"),
            "last_check_ts": now,
            "consecutive_errors": st.get("consecutiveErrors", 0),
            "last_run_status": st.get("lastRunStatus"),
            "is_failing_now": jid in failing,
        }

def _format_telegram(findings: List[Dict[str, Any]], tier1_done: List[str],
                     claude: Optional[Dict[str, Any]], report_path: Path) -> str:
    n_tier1 = sum(1 for f in findings if f.get("severity") == "tier1")
    n_tier2 = sum(1 for f in findings if f.get("severity") == "tier2")
    n_block = sum(1 for f in findings if f.get("severity") == "block")
    lines = [f"🔍 *Hourly health sweep* — {n_tier1} tier1, {n_tier2} tier2, {n_block} block"]
    if tier1_done:
        lines.append("*Auto-fixes applied*:")
        for a in tier1_done[:5]:
            lines.append(f"  • {a}")
    if claude:
        if claude.get("ok"):
            report = (claude.get("report") or "")
            m = re.search(r"## SUMMARY\s*\n+(.+)", report, re.S)
            summary = m.group(1).strip()[:700] if m else report[:700]
            lines.append(f"*Claude diagnosis* ({claude.get('turns','?')} turns, "
                         f"{claude.get('duration_s','?')}s):")
            lines.append(summary)
        else:
            lines.append(f"*Claude*: failed — {claude.get('error','')}")
    lines.append(f"_Report: {report_path.name}_")
    return "\n".join(lines)

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _log("interrupted")
        sys.exit(130)
    except (OSError, subprocess.SubprocessError) as e:
        _log(f"fatal: {e}")
        sys.exit(1)
