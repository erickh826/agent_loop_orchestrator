#!/usr/bin/env python3
"""
Loop Engineer Orchestrator — Windows Native, multi-project
==========================================================
One orchestrator install drives many projects (anywhere on C:/D:). Each `run`
executes exactly ONE phase for ONE project:

  01_planning → 02_implement → 03_qa_review →
      (04_refactor ↔ 03_qa_review, at most MAX_REFACTOR_RETRIES times)
  → 05_done | 06_failed_requires_human

Commands:
  run --project N [--agent A]   run one phase for project N (optionally with agent A, this run only)
  status [--project N] [--json] show phase / retries / last_run
  reset --project N [--phase P] back into the loop: phase P (default 03_qa_review), retries 0
  register N PATH [--media P] [--copy-prompts-from M]
  assign --project N PHASE=AGENT ... | --clear PHASE|all   per-project phase -> agent
  agents [--json]               list agent CLIs (agents.json) and effective phase mapping
  gui add|next|done|fail|list   GUI task queue (gate only — see loop_gui.py)
  here on|off                   create/remove i_am_here.flag
  --dry-run (global)            print agent command + cwd instead of running it

Per-project files:  <project>/.loop/{state.json, orchestrator.log, run.lock, runs/, agents/}
Agent outputs stay in <project>/tasks/ and <project>/memo/ — the prompts address
them as {{WORKSPACE}}/tasks/... and {{WORKSPACE}}/memo/....

Subprocess rules (unchanged from the single-project version):
  - shell=False + list-form args, so multi-line prompts reach the CLI as one
    literal argument instead of being re-parsed (and silently truncated at the
    first newline) by cmd.exe
  - stdin=DEVNULL to prevent CLI tools from hanging on non-TTY
  - encoding="utf-8", errors="replace"
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from loop_common import (
    EXIT_ERROR, EXIT_OK, EXIT_USAGE, IS_WINDOWS, ORCH_DIR, LoopError, Project, RunLock,
    fwd, get_project, is_batch, load_projects, loop_home, projects_file, read_json, resolve_bin,
    save_projects, setup_logging, validate_name, write_json_atomic,
)
import loop_agents
import loop_gui
from loop_agents import PHASES, AgentConfig

MAX_REFACTOR_RETRIES = 3

TERMINAL_PHASES = {"05_done", "06_failed_requires_human"}

# Which CLI runs each phase, how it is invoked and its timeout: see agents.json / loop_agents.py.

EXPECTED_OUTPUT = {
    "01_planning":  ("01_planning",  "spec.md"),
    "02_implement": ("02_implement", "implementation_notes.md"),
    "03_qa_review": ("03_qa_review", "qa_report.md"),
    "04_refactor":  ("04_refactor",  "refactor_log.md"),
}

log = logging.getLogger("loop")


class PhaseFailed(LoopError):
    pass


# ─────────────────────────────────────────────
#  State Management
# ─────────────────────────────────────────────
def default_state() -> dict:
    return {
        "phase": "01_planning",
        "refactor_retries": 0,
        "last_run": None,
        "status": "running",
    }


def migrate_legacy_state(project: Project, dry_run: bool) -> None:
    """Move <project>/state.json (single-project layout) to <project>/.loop/state.json."""
    legacy, target = project.legacy_state_file, project.state_file
    if not legacy.exists() or target.exists():
        return
    if dry_run:
        log.info(f"[dry-run] Would migrate legacy state {legacy} -> {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy), str(target))
    log.info(f"Migrated legacy state file {legacy} -> {target}")


def load_state(project: Project, dry_run: bool = False) -> dict:
    sf = project.state_file
    if not sf.exists() and dry_run and project.legacy_state_file.exists():
        # Dry-run did not migrate; show what the real run would use.
        return read_json(project.legacy_state_file)
    if not sf.exists():
        state = default_state()
        if not dry_run:
            save_state(project, state)
        return state
    return read_json(sf)


def save_state(project: Project, state: dict) -> None:
    state["last_run"] = datetime.now().isoformat()
    write_json_atomic(project.state_file, state)
    log.info(f"State saved: phase={state['phase']}, retries={state.get('refactor_retries', 0)}")


# ─────────────────────────────────────────────
#  Prompt Loading
# ─────────────────────────────────────────────
def load_prompt(project: Project, agent: str, phase: str, cfg: AgentConfig) -> str:
    """Agent-specific prompt first, then the prompt written for this phase's default agent,
    then any other agent's prompt for this phase — project .loop/agents/ before global agents/."""
    default_agent = cfg.default_phases[phase]
    candidates = []
    for base in (project.agents_dir, ORCH_DIR / "agents"):
        candidates += [base / agent / f"{phase}.md", base / default_agent / f"{phase}.md"]
        if base.is_dir():
            candidates += sorted(base.glob(f"*/{phase}.md"))
    seen, ordered = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    candidates = ordered
    prompt_file = next((p for p in candidates if p.is_file()), None)
    if prompt_file is None:
        tried = "\n  ".join(str(p) for p in candidates)
        raise LoopError(
            f"Prompt file not found: {agent}/{phase}.md\n  tried:\n  {tried}\n"
            f"  Add it to {project.agents_dir} (or use `register --copy-prompts-from`)."
        )
    log.info(f"Prompt: {prompt_file}")
    template = prompt_file.read_text(encoding="utf-8")
    text = template.replace("{{WORKSPACE}}", fwd(project.path))
    if "{{MEDIA}}" in text:
        if project.media is None:
            log.warning(f"{prompt_file} uses {{{{MEDIA}}}} but project '{project.name}' has no media path")
        text = text.replace("{{MEDIA}}", fwd(project.media) if project.media else "")
    return text


# ─────────────────────────────────────────────
#  Agent Runners
#  shell=False + list-form args → Python builds the Windows command line itself
#  (via CreateProcess) instead of handing a single string to cmd.exe to re-parse.
#  Our prompts are multi-line markdown; cmd.exe treats embedded newlines as
#  command separators, so a shell=True string would silently truncate them.
#  DEVNULL → prevents CLI tools hanging waiting for stdin.
# ─────────────────────────────────────────────
class Runner:
    def __init__(self, project: Project, phase: str, dry_run: bool, cfg: AgentConfig, agent: str):
        self.project = project
        self.phase = phase
        self.dry_run = dry_run
        self.cfg = cfg
        self.agent = agent

    def _bin(self, name: str) -> list[str]:
        try:
            return resolve_bin(name)
        except LoopError as e:
            if not self.dry_run:
                raise
            log.warning(f"[dry-run] {e}")
            return [f"<NOT FOUND: {name}>"]

    def run_agent(self, prompt: str) -> subprocess.CompletedProcess | None:
        defn = self.cfg.get(self.agent)
        log.info(f"Dispatching to: {self.agent} (phase {self.phase}, prompt_mode {defn.prompt_mode})")
        started = datetime.now()
        prompt_file = self.project.runs_dir / f"{_stamp(started)}_{self.phase}_{self.agent}.prompt.md"
        if self.dry_run:
            print(f"[dry-run] prompt  : would be saved to {prompt_file}")
        else:
            # Always kept for audit; for prompt_mode file/pointer the agent reads it.
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(prompt, encoding="utf-8")
        cmd = [*self._bin(defn.bin), *loop_agents.build_args(defn, self.project, prompt, prompt_file)]
        return self.run(self.agent, cmd, defn.timeout, started)

    def run(self, agent: str, cmd: list[str], timeout: int,
            started: datetime | None = None) -> subprocess.CompletedProcess | None:
        cwd = self.project.path
        if is_batch(cmd[0]) and any("\n" in a for a in cmd[1:]):
            # cmd.exe would cut the prompt at its first newline — refuse rather than
            # let the agent silently run on a one-line prompt.
            raise PhaseFailed(
                f"{cmd[0]} is a .cmd/.bat that could not be unwrapped to node + script; "
                "a multi-line prompt would be truncated by cmd.exe. Point PATH at a real executable."
            )
        if self.dry_run:
            shown = [(_abbrev(a) if "\n" in a else a) for a in cmd]
            print(f"[dry-run] agent   : {agent} (phase {self.phase})")
            print(f"[dry-run] cwd     : {cwd}")
            print(f"[dry-run] timeout : {timeout}s")
            print(f"[dry-run] command : {json.dumps(shown, ensure_ascii=False)}")
            log.info(f"[dry-run] {agent} not executed; state will not advance")
            return None

        started = started or datetime.now()
        try:
            result = _run(cmd, cwd, timeout)
        except subprocess.TimeoutExpired as e:
            log.error(
                f"[{agent}] TIMEOUT in phase {self.phase} after {timeout}s — "
                "process tree killed, state NOT advanced"
            )
            self._save_full_output(agent, started, cmd, None, _text(e.output), _text(e.stderr),
                                   note=f"TIMEOUT after {timeout}s")
            raise PhaseFailed(f"{agent} timed out after {timeout}s in phase {self.phase}")
        _log_agent_output(agent, result, self._save_full_output(
            agent, started, cmd, result.returncode, result.stdout, result.stderr))
        return result

    def _save_full_output(self, agent, started, cmd, returncode, stdout, stderr, note="") -> Path:
        runs = self.project.runs_dir
        runs.mkdir(parents=True, exist_ok=True)
        path = runs / f"{_stamp(started)}_{self.phase}_{agent}.log"
        shown = [(_abbrev(a) if "\n" in a else a) for a in cmd]
        path.write_text(
            f"agent      : {agent}\nphase      : {self.phase}\nstarted    : {started.isoformat()}\n"
            f"finished   : {datetime.now().isoformat()}\ncwd        : {self.project.path}\n"
            f"command    : {json.dumps(shown, ensure_ascii=False)}\nexit code  : {returncode}\n"
            + (f"note       : {note}\n" if note else "")
            + f"\n===== STDOUT =====\n{stdout or ''}\n\n===== STDERR =====\n{stderr or ''}\n",
            encoding="utf-8",
        )
        return path


def _stamp(t: datetime) -> str:
    return f"{t:%Y%m%d-%H%M%S}-{t.microsecond // 1000:03d}"


def _run(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # .cmd/.bat wrappers spawn node as a grandchild that keeps the pipes
        # open; killing only the shim would leave communicate() hanging. Kill the tree.
        _kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", "(output unavailable: pipes still held after kill)"
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _kill_tree(proc: subprocess.Popen) -> None:
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True, stdin=subprocess.DEVNULL, shell=False,
            encoding="utf-8", errors="replace",
        )
    else:
        proc.kill()


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return v


def _abbrev(s: str, n: int = 60) -> str:
    first = s.strip().splitlines()[0] if s.strip() else ""
    return f"<prompt: {len(s)} chars, {s.count(chr(10)) + 1} lines: {first[:n]!r}...>"


def _log_agent_output(agent: str, result: subprocess.CompletedProcess, full_log: Path):
    if result.stdout:
        log.info(f"[{agent}] stdout:\n{result.stdout[:2000]}")
    if result.stderr:
        log.warning(f"[{agent}] stderr:\n{result.stderr[:1000]}")
    log.info(f"[{agent}] exit code: {result.returncode}  (full output: {full_log})")


# ─────────────────────────────────────────────
#  Output Validation
# ─────────────────────────────────────────────
def expected_output(project: Project, phase: str) -> Path | None:
    entry = EXPECTED_OUTPUT.get(phase)
    return project.tasks_dir / entry[0] / entry[1] if entry else None


def validate_output(project: Project, phase: str, phase_start: float) -> bool:
    target = expected_output(project, phase)
    if target is None:
        return True
    if not target.exists():
        log.warning(f"Expected output not found: {target}")
        return False
    st = target.stat()
    if st.st_mtime <= phase_start:
        log.warning(
            f"Expected output {target} was not updated during this phase "
            f"(mtime {datetime.fromtimestamp(st.st_mtime).isoformat()} <= phase start "
            f"{datetime.fromtimestamp(phase_start).isoformat()}) — stale output from a previous run"
        )
        return False
    if st.st_size == 0:
        log.warning(f"Expected output {target} is empty")
        return False
    if phase == "01_planning":
        lines = target.read_text(encoding="utf-8").splitlines()
        first = lines[0] if lines else ""
        if "STOPPED" in first:
            log.warning(
                f"{target} is a STOP notice (task scope not filled in), not a real plan. "
                "Fill in memo/project_context.md's 'This Loop Run's Task Scope' and re-run."
            )
            return False
    return True


def qa_passed(project: Project) -> bool:
    qa_report = expected_output(project, "03_qa_review")
    if not qa_report.exists():
        return False
    lines = [line.strip() for line in qa_report.read_text(encoding="utf-8").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("STATUS:"):
            return line == "STATUS: PASSED"
    return False


# ─────────────────────────────────────────────
#  Phase Handlers — each returns an exit code
# ─────────────────────────────────────────────
def _dispatch(runner: Runner, project: Project, phase: str) -> bool:
    """Run the selected agent for this phase; True if it succeeded and produced fresh output."""
    prompt = load_prompt(project, runner.agent, phase, runner.cfg)
    phase_start = time.time()
    result = runner.run_agent(prompt)
    if result is None:  # dry-run
        return False
    return result.returncode == 0 and validate_output(project, phase, phase_start)


def handle_planning(project: Project, state: dict, runner: Runner) -> int:
    if not _dispatch(runner, project, "01_planning"):
        return _not_advanced(runner, "Planning")
    state["phase"] = "02_implement"
    save_state(project, state)
    return EXIT_OK


def handle_implement(project: Project, state: dict, runner: Runner) -> int:
    if not _dispatch(runner, project, "02_implement"):
        return _not_advanced(runner, "Implementation")
    state["phase"] = "03_qa_review"
    save_state(project, state)
    return EXIT_OK


def handle_qa_review(project: Project, state: dict, runner: Runner) -> int:
    if not _dispatch(runner, project, "03_qa_review"):
        return _not_advanced(runner, "QA review")
    if qa_passed(project):
        log.info("QA PASSED. Moving to 05_done.")
        state.update({"phase": "05_done", "status": "completed", "refactor_retries": 0})
        save_state(project, state)
        _finalize_done(project)
    else:
        retries = state.get("refactor_retries", 0)
        if retries >= MAX_REFACTOR_RETRIES:
            log.error(f"Max refactor retries ({MAX_REFACTOR_RETRIES}) reached. Halting.")
            state.update({"phase": "06_failed_requires_human", "status": "halted"})
            save_state(project, state)
        else:
            log.info(f"QA FAILED. Moving to refactor (attempt {retries + 1}/{MAX_REFACTOR_RETRIES}).")
            state.update({"phase": "04_refactor", "refactor_retries": retries + 1})
            save_state(project, state)
    return EXIT_OK


def handle_refactor(project: Project, state: dict, runner: Runner) -> int:
    if not _dispatch(runner, project, "04_refactor"):
        return _not_advanced(runner, "Refactor")
    log.info("Refactor complete. Returning to QA review.")
    old = expected_output(project, "03_qa_review")
    if old.exists():
        old.unlink()
    state["phase"] = "03_qa_review"
    save_state(project, state)
    return EXIT_OK


def _not_advanced(runner: Runner, label: str) -> int:
    if runner.dry_run:
        return EXIT_OK
    log.error(f"{label} phase failed. Run again to retry.")
    return EXIT_ERROR


def _finalize_done(project: Project):
    summary = project.tasks_dir / "05_done" / "SUMMARY.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        f"# Project Complete\n\nCompleted at: {datetime.now().isoformat()}\n\n"
        "All phases passed. See /tasks/ for phase-by-phase documentation.\n",
        encoding="utf-8",
    )
    log.info(f"Final summary written to {summary}")


DISPATCH = {
    "01_planning":  handle_planning,
    "02_implement": handle_implement,
    "03_qa_review": handle_qa_review,
    "04_refactor":  handle_refactor,
}


# ─────────────────────────────────────────────
#  Commands
# ─────────────────────────────────────────────
def cmd_run(name: str, dry_run: bool, agent_override: str | None = None) -> int:
    project = get_project(name)
    cfg = loop_agents.load_config()
    setup_logging(project.log_file, sys.stdout)
    log.info("=" * 60)
    log.info(f"Loop Engineer Orchestrator — project '{name}'{' [DRY RUN]' if dry_run else ''}")
    log.info(f"Project path: {project.path}")
    log.info("=" * 60)

    with RunLock(project.lock_file, f"Project '{name}'"):
        migrate_legacy_state(project, dry_run)
        state = load_state(project, dry_run)
        phase = state.get("phase", "01_planning")

        # End-loop guard
        if phase in TERMINAL_PHASES:
            log.info(f"Project is already in terminal phase '{phase}'. Nothing to do.")
            print(f"\n{'=' * 50}")
            print(f"  PROJECT: {name}")
            print(f"  STATUS : {phase.upper()}")
            print(f"  No action taken.")
            print(f"{'=' * 50}\n")
            return EXIT_OK

        log.info(f"Current phase: {phase}")
        handler = DISPATCH.get(phase)
        if handler is None:
            log.error(f"Unknown phase: '{phase}'. Check {project.state_file} manually.")
            return EXIT_ERROR

        agent, source = loop_agents.resolve_agent(cfg, project, phase, agent_override)
        log.info(f"Agent for {phase}: {agent} (from {source})")
        runner = Runner(project, phase, dry_run, cfg, agent)
        try:
            code = handler(project, state, runner)
        except PhaseFailed as e:
            log.error(f"{e}. Run again to retry.")
            code = e.code

        updated = load_state(project, dry_run)
        print(f"\n{'=' * 50}")
        print(f"  PROJECT    : {name}{'  [DRY RUN]' if dry_run else ''}")
        print(f"  RAN PHASE  : {phase}  (agent: {agent})")
        print(f"  NEXT PHASE : {updated['phase']}")
        print(f"  RETRIES    : {updated.get('refactor_retries', 0)}/{MAX_REFACTOR_RETRIES}")
        print(f"  LOG FILE   : {project.log_file}")
        print(f"  EXIT CODE  : {code}")
        print(f"{'=' * 50}\n")
        log.info("Run complete.\n")
        return code


def cmd_reset(name: str, phase: str, dry_run: bool) -> int:
    """Put a project back into the loop: phase=<phase>, refactor_retries=0, status=running."""
    project = get_project(name)
    setup_logging(project.log_file, sys.stdout)
    with RunLock(project.lock_file, f"Project '{name}'"):
        migrate_legacy_state(project, dry_run)
        sf = project.state_file
        if not sf.exists() and dry_run and project.legacy_state_file.exists():
            sf = project.legacy_state_file
        old = read_json(sf) if sf.exists() else None
        new = dict(old or default_state())
        new.update({"phase": phase, "refactor_retries": 0, "status": "running"})
        before = "(no state)" if old is None else (
            f"phase={old.get('phase')}, retries={old.get('refactor_retries')}, status={old.get('status')}")
        if dry_run:
            log.info(f"[dry-run] Would reset '{name}': {before} -> phase={phase}, retries=0, status=running")
            return EXIT_OK
        log.info(f"Reset '{name}': {before} -> phase={phase}, retries=0, status=running")
        save_state(project, new)
    print(f"reset: {name} -> {phase} (retries 0)")
    return EXIT_OK


def project_status(name: str, entry: dict, cfg: AgentConfig | None) -> dict:
    info = {"project": name, "path": entry.get("path"), "media": entry.get("media"),
            "phase": None, "refactor_retries": None, "max_retries": MAX_REFACTOR_RETRIES,
            "last_run": None, "status": None, "running": False, "state_file": None,
            "agents": cfg.effective_phases(entry.get("phases")) if cfg else None,
            "agent_overrides": entry.get("phases") or {}, "error": None}
    path = Path(entry.get("path", ""))
    if not path.is_dir():
        info["error"] = "project path not found"
        return info
    project = Project(name, path, Path(entry["media"]) if entry.get("media") else None)
    sf = project.state_file
    if not sf.exists() and project.legacy_state_file.exists():
        sf = project.legacy_state_file  # not migrated yet; status stays read-only
    if sf.exists():
        try:
            state = read_json(sf)
            info.update({k: state.get(k) for k in ("phase", "refactor_retries", "last_run", "status")})
            info["state_file"] = str(sf)
        except (OSError, ValueError) as e:
            info["error"] = f"cannot read {sf}: {e}"
    info["running"] = RunLock(project.lock_file, name).is_held()
    return info


def cmd_status(name: str | None, as_json: bool) -> int:
    projects = load_projects()
    if name is not None:
        get_project(name)  # validates registration
        names = [name]
    else:
        names = sorted(projects)
    try:
        cfg = loop_agents.load_config()
    except LoopError as e:
        log.warning(f"status: agent mapping unavailable: {e}")
        cfg = None
    rows = [project_status(n, projects[n], cfg) for n in names]

    if as_json:
        print(json.dumps({"loop_home": str(loop_home()), "projects": rows}, indent=2, ensure_ascii=False))
        return EXIT_OK
    if not rows:
        print(f"(no projects registered in {projects_file()})")
        return EXIT_OK
    print(f"{'PROJECT':<14} {'PHASE':<26} {'RETRIES':<8} {'RUNNING':<8} LAST_RUN")
    for r in rows:
        phase = r["phase"] or ("(no state)" if not r["error"] else f"ERROR: {r['error']}")
        if r["state_file"] and not r["state_file"].replace("\\", "/").endswith("/.loop/state.json"):
            phase += " (legacy)"
        retries = "-" if r["refactor_retries"] is None else f"{r['refactor_retries']}/{MAX_REFACTOR_RETRIES}"
        print(f"{r['project']:<14} {phase:<26} {retries:<8} {'yes' if r['running'] else 'no':<8} "
              f"{r['last_run'] or '-'}")
    return EXIT_OK


def cmd_register(name: str, path: str, media: str | None, copy_from: str | None) -> int:
    validate_name(name)
    proj_path = Path(path).expanduser().resolve()
    if not proj_path.is_dir():
        raise LoopError(f"Project path does not exist or is not a directory: {proj_path}", EXIT_USAGE)
    media_path = None
    if media:
        media_path = Path(media).expanduser().resolve()
        if not media_path.is_dir():
            raise LoopError(f"Media path does not exist or is not a directory: {media_path}", EXIT_USAGE)

    source = None
    if copy_from:
        source = get_project(copy_from)
        if not source.agents_dir.is_dir():
            raise LoopError(f"Project '{copy_from}' has no prompts to copy ({source.agents_dir})")

    projects = load_projects()
    existed = name in projects
    entry = {"path": fwd(proj_path)}
    if media_path:
        entry["media"] = fwd(media_path)
    if existed and projects[name].get("phases"):
        entry["phases"] = projects[name]["phases"]  # keep agent assignments on re-register
    projects[name] = entry
    save_projects(projects)

    project = Project(name, proj_path, media_path)
    for d in (project.loop_dir, project.runs_dir, project.agents_dir):
        d.mkdir(parents=True, exist_ok=True)
    log.info(f"{'Updated' if existed else 'Registered'} project '{name}': {entry} in {projects_file()}")
    print(f"{'updated' if existed else 'registered'}: {name} -> {entry['path']}"
          + (f" (media {entry['media']})" if media_path else ""))

    if source is not None:
        copied, skipped = 0, 0
        for src in sorted(source.agents_dir.rglob("*")):
            if not src.is_file():
                continue
            dst = project.agents_dir / src.relative_to(source.agents_dir)
            if dst.exists():
                skipped += 1
                log.warning(f"Not overwriting existing prompt {dst}")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        print(f"prompts copied from '{copy_from}': {copied} copied, {skipped} skipped (already present)")

    if project.legacy_state_file.exists() and not project.state_file.exists():
        print(f"note: legacy {project.legacy_state_file} will be migrated to .loop/ on the first real run")
    return EXIT_OK


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────
def cmd_assign(name: str, pairs: list[str], clear: list[str] | None) -> int:
    get_project(name)  # validates registration
    cfg = loop_agents.load_config()
    projects = load_projects()
    phases = dict(projects[name].get("phases") or {})

    for item in clear or []:
        if item == "all":
            phases.clear()
        elif item in PHASES:
            phases.pop(item, None)
        else:
            raise LoopError(f"--clear: unknown phase '{item}' (allowed: {', '.join(PHASES)}, all)", EXIT_USAGE)
    updates = {}
    for pair in pairs:
        phase, sep, agent = pair.partition("=")
        if not sep or not phase or not agent:
            raise LoopError(f"Expected PHASE=AGENT, got '{pair}'", EXIT_USAGE)
        updates[phase.strip()] = agent.strip()
    loop_agents.validate_overrides(cfg, updates)
    phases.update(updates)

    if phases:
        projects[name]["phases"] = {p: phases[p] for p in PHASES if p in phases}
    else:
        projects[name].pop("phases", None)
    save_projects(projects)
    log.info(f"assign '{name}': overrides now {projects[name].get('phases', {})}")
    effective = cfg.effective_phases(phases)
    for phase in PHASES:
        mark = "(project)" if phase in phases else "(default)"
        print(f"{name:<14} {phase:<14} {effective[phase]:<10} {mark}")
    return EXIT_OK


def cmd_agents(as_json: bool) -> int:
    cfg = loop_agents.load_config()
    projects = load_projects()
    agents = []
    for name, d in cfg.agents.items():
        try:
            resolved = resolve_bin(d.bin)
        except LoopError:
            resolved = None
        agents.append({"agent": name, "bin": d.bin, "resolved": resolved, "prompt_mode": d.prompt_mode,
                       "timeout": d.timeout, "note": d.note})
    mapping = {n: {"effective": cfg.effective_phases(e.get("phases")), "overrides": e.get("phases") or {}}
               for n, e in sorted(projects.items())}
    if as_json:
        print(json.dumps({"agents_file": str(loop_agents.AGENTS_FILE), "agents": agents,
                          "default_phases": cfg.default_phases, "projects": mapping},
                         indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"Agents ({loop_agents.AGENTS_FILE}):")
    print(f"  {'AGENT':<10} {'MODE':<8} {'TIMEOUT':<8} RESOLVED")
    for a in agents:
        resolved = " ".join(a["resolved"]) if a["resolved"] else "NOT FOUND on PATH"
        print(f"  {a['agent']:<10} {a['prompt_mode']:<8} {str(a['timeout']) + 's':<8} {resolved}")
    print("\nPhase -> agent (* = project override):")
    print(f"  {'PROJECT':<14} " + " ".join(f"{p:<14}" for p in PHASES))
    print(f"  {'(default)':<14} " + " ".join(f"{cfg.default_phases[p]:<14}" for p in PHASES))
    for n, m in mapping.items():
        cells = [(m["effective"][p] + ("*" if p in m["overrides"] else "")) for p in PHASES]
        print(f"  {n:<14} " + " ".join(f"{c:<14}" for c in cells))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="loop_orchestrator.py", description="Loop Engineer multi-project orchestrator")
    p.add_argument("--dry-run", action="store_true", help="print agent command + cwd instead of running it")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run one phase for a project")
    r.add_argument("--project", required=True)
    r.add_argument("--agent", help="use this agent (from agents.json) for this run only")

    rs = sub.add_parser("reset", help="put a project back into the loop (retries 0, status running)")
    rs.add_argument("--project", required=True)
    rs.add_argument("--phase", default="03_qa_review", choices=list(DISPATCH),
                    help="phase to restart from (default: 03_qa_review)")

    s = sub.add_parser("status", help="show project phase / retries / last_run")
    s.add_argument("--project")
    s.add_argument("--json", action="store_true", help="machine-readable output")

    g = sub.add_parser("register", help="register a project and create its .loop/")
    g.add_argument("name")
    g.add_argument("path")
    g.add_argument("--media")
    g.add_argument("--copy-prompts-from", metavar="PROJECT")

    gui = sub.add_parser("gui", help="GUI task queue")
    gsub = gui.add_subparsers(dest="gui_command", required=True)
    ga = gsub.add_parser("add")
    ga.add_argument("--project", required=True)
    ga.add_argument("instructions")
    gsub.add_parser("next")
    gd = gsub.add_parser("done")
    gd.add_argument("id")
    gd.add_argument("--result")
    gf = gsub.add_parser("fail")
    gf.add_argument("id")
    gf.add_argument("--reason", required=True)
    gsub.add_parser("list")

    a = sub.add_parser("assign", help="set which agent runs each phase for a project")
    a.add_argument("--project", required=True)
    a.add_argument("pairs", nargs="*", metavar="PHASE=AGENT")
    a.add_argument("--clear", nargs="+", metavar="PHASE", help="remove overrides (PHASE ... or all)")

    ag = sub.add_parser("agents", help="list agent CLIs and the effective phase -> agent mapping")
    ag.add_argument("--json", action="store_true")

    h = sub.add_parser("here", help="mark whether the owner is using this computer")
    h.add_argument("state", choices=["on", "off"])
    return p


def main(argv: list[str] | None = None) -> int:
    # Console output may be redirected by a remote trigger; never die on encoding.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    if args.command not in ("run", "reset"):
        setup_logging(loop_home() / "loop.log")
    try:
        if args.command == "run":
            return cmd_run(args.project, args.dry_run, args.agent)
        if args.command == "assign":
            if not args.pairs and not args.clear:
                raise LoopError("assign: give PHASE=AGENT pairs and/or --clear", EXIT_USAGE)
            return cmd_assign(args.project, args.pairs, args.clear)
        if args.command == "agents":
            return cmd_agents(args.json)
        if args.command == "reset":
            return cmd_reset(args.project, args.phase, args.dry_run)
        if args.command == "status":
            return cmd_status(args.project, args.json)
        if args.command == "register":
            return cmd_register(args.name, args.path, args.media, args.copy_prompts_from)
        if args.command == "here":
            return loop_gui.cmd_here(args.state == "on")
        if args.command == "gui":
            gc = args.gui_command
            if gc == "add":
                return loop_gui.cmd_add(args.project, args.instructions)
            if gc == "next":
                return loop_gui.cmd_next()
            if gc == "done":
                return loop_gui.cmd_done(args.id, args.result)
            if gc == "fail":
                return loop_gui.cmd_fail(args.id, args.reason)
            if gc == "list":
                return loop_gui.cmd_list()
        return EXIT_USAGE
    except LoopError as e:
        if not logging.getLogger().handlers:
            setup_logging(None)
        log.error(str(e))
        return e.code


if __name__ == "__main__":
    sys.exit(main())
