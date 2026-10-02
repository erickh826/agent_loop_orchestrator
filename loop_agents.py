"""
Agent CLI definitions (agents.json) and phase → agent selection.

agents.json lives next to the code (tracked in git):

  {
    "agents": {
      "<name>": {
        "bin": "<executable on PATH>",
        "args": ["...", "{prompt}", ...],     # tokens: {prompt} {prompt_file} {project} {media}
        "dir_args": ["--add-dir", "{dir}"],   # optional, per extra dir (media); placed at "{dir_args}" in args, else appended
        "timeout": 1200,                       # outer subprocess timeout, seconds
        "prompt_mode": "arg" | "file" | "pointer",   # optional, default "arg"
        "note": "free text"                    # optional
      }
    },
    "default_phases": {"01_planning": "<name>", "02_implement": ..., "03_qa_review": ..., "04_refactor": ...}
  }

prompt_mode:
  arg      the full prompt is passed as {prompt}
  file     the prompt is written to a file, passed as {prompt_file}
  pointer  {prompt} is a one-line "read <file> and follow it" instruction — for CLIs
           that are .bat/.ps1 wrappers, where cmd.exe would cut a multi-line argument

Selection precedence (highest first):  run --agent  >  projects.json "phases"  >  default_phases
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from loop_common import EXIT_USAGE, ORCH_DIR, LoopError, Project, fwd, read_json

AGENTS_FILE = ORCH_DIR / "agents.json"
PHASES = ("01_planning", "02_implement", "03_qa_review", "04_refactor")
PROMPT_MODES = ("arg", "file", "pointer")
_TOKEN_RE = re.compile(r"\{(prompt|prompt_file|project|media|dir)\}")


@dataclass
class AgentDef:
    name: str
    bin: str
    args: list[str]
    timeout: int
    dir_args: list[str] = field(default_factory=list)
    prompt_mode: str = "arg"
    note: str = ""


@dataclass
class AgentConfig:
    agents: dict[str, AgentDef]
    default_phases: dict[str, str]

    def get(self, name: str) -> AgentDef:
        if name not in self.agents:
            raise LoopError(
                f"Unknown agent '{name}'. Defined in {AGENTS_FILE}: {', '.join(sorted(self.agents))}",
                EXIT_USAGE,
            )
        return self.agents[name]

    def effective_phases(self, overrides: dict | None) -> dict[str, str]:
        phases = dict(self.default_phases)
        phases.update(overrides or {})
        return phases


def load_config() -> AgentConfig:
    if not AGENTS_FILE.is_file():
        raise LoopError(f"Agent definitions not found: {AGENTS_FILE}")
    try:
        raw = read_json(AGENTS_FILE)
    except ValueError as e:
        raise LoopError(f"{AGENTS_FILE} is not valid JSON: {e}")

    def bad(msg: str):
        return LoopError(f"{AGENTS_FILE}: {msg}")

    agents = {}
    for name, d in (raw.get("agents") or {}).items():
        if not isinstance(d, dict) or not isinstance(d.get("bin"), str) or not d["bin"]:
            raise bad(f"agent '{name}' needs a non-empty \"bin\"")
        args = d.get("args")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise bad(f"agent '{name}': \"args\" must be a list of strings")
        dir_args = d.get("dir_args", [])
        if not isinstance(dir_args, list) or not all(isinstance(a, str) for a in dir_args):
            raise bad(f"agent '{name}': \"dir_args\" must be a list of strings")
        timeout = d.get("timeout")
        if not isinstance(timeout, int) or timeout <= 0:
            raise bad(f"agent '{name}': \"timeout\" must be a positive integer (seconds)")
        mode = d.get("prompt_mode", "arg")
        if mode not in PROMPT_MODES:
            raise bad(f"agent '{name}': \"prompt_mode\" must be one of {PROMPT_MODES}")
        needed = "{prompt_file}" if mode == "file" else "{prompt}"
        if not any(needed in a for a in args):
            raise bad(f"agent '{name}': prompt_mode '{mode}' requires {needed} in \"args\"")
        agents[name] = AgentDef(name, d["bin"], args, timeout, dir_args, mode, d.get("note", ""))

    default_phases = raw.get("default_phases") or {}
    for phase in PHASES:
        if default_phases.get(phase) not in agents:
            raise bad(f"default_phases['{phase}'] must name a defined agent")
    return AgentConfig(agents, {p: default_phases[p] for p in PHASES})


def validate_overrides(cfg: AgentConfig, overrides: dict) -> None:
    for phase, agent in overrides.items():
        if phase not in PHASES:
            raise LoopError(f"Unknown phase '{phase}' (allowed: {', '.join(PHASES)})", EXIT_USAGE)
        cfg.get(agent)


def resolve_agent(cfg: AgentConfig, project: Project, phase: str, override: str | None) -> tuple[str, str]:
    """Return (agent name, where the choice came from)."""
    if override:
        cfg.get(override)
        return override, "--agent"
    if phase in project.phases:
        cfg.get(project.phases[phase])
        return project.phases[phase], "project"
    return cfg.default_phases[phase], "default"


def pointer_text(prompt_file: Path) -> str:
    # Single line, no cmd.exe metacharacters of our own — survives .bat/.ps1 wrappers.
    return (f"Your complete task instructions are in the file {fwd(prompt_file)} . "
            "Read that whole file first and follow its instructions exactly.")


def build_args(defn: AgentDef, project: Project, prompt: str, prompt_file: Path) -> list[str]:
    """Substitute tokens in one pass, so text inside the prompt is never re-expanded."""
    values = {
        "prompt": prompt if defn.prompt_mode == "arg" else pointer_text(prompt_file),
        "prompt_file": str(prompt_file),
        "project": str(project.path),
        "media": str(project.media) if project.media else "",
    }

    def sub(arg: str, extra: dict | None = None) -> str:
        table = {**values, **(extra or {})}
        if arg == "{prompt}":
            return table["prompt"]
        return _TOKEN_RE.sub(lambda m: table.get(m.group(1), m.group(0)), arg)

    extra = []
    if project.media is not None:
        extra = [sub(a, {"dir": str(project.media)}) for a in defn.dir_args]
    args = []
    placed = False
    for a in defn.args:
        if a == "{dir_args}":  # explicit position for the extra-dir args
            args += extra
            placed = True
        else:
            args.append(sub(a))
    if not placed:
        args += extra
    return args
