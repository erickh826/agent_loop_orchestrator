#!/usr/bin/env python3
"""
Smoke test for the agent CLIs in agents.json — runs each one through the real
orchestrator Runner (same command building, prompt_mode handling, shim unwrapping,
timeout handling) on a tiny task in a throwaway project.

Each agent must, within --timeout seconds and without asking anything:
  1. read a token from the media dir           -> MEDIA_READ   (dir_args / media access)
  2. write <work>/smoke proj/smoke_out/<agent>.txt (absolute path)
  3. put the PROMPT TOKEN from the LAST line of the prompt on line 2
                                               -> PROMPT_INTACT (multi-line prompt not truncated)

WARNING: this calls the real CLIs (costs tokens/quota) with their auto-approve flags.
Run it in a throwaway --workdir only.

  python tests/smoke_agents.py --workdir C:/tmp/loop-smoke                 # all agents
  python tests/smoke_agents.py --workdir C:/tmp/loop-smoke kimi gemini     # selected agents
"""

import argparse
import json
import secrets
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import loop_agents  # noqa: E402
import loop_orchestrator as lo  # noqa: E402
from loop_common import LoopError, Project, setup_logging  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", required=True, help="throwaway directory (a 'smoke proj' and 'media dir' are created in it)")
    ap.add_argument("--timeout", type=int, default=300, help="per-agent timeout in seconds (default 300)")
    ap.add_argument("agents", nargs="*", help="agents to test (default: all in agents.json)")
    args = ap.parse_args()

    work = Path(args.workdir).resolve()
    proj_dir, media_dir = work / "smoke proj", work / "media dir"   # spaces on purpose
    proj_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    media_token = f"MEDIA-{secrets.token_hex(4)}"
    (media_dir / "media_token.txt").write_text(media_token + "\n", encoding="utf-8")
    setup_logging(proj_dir / ".loop" / "orchestrator.log")

    cfg = loop_agents.load_config()
    agents = args.agents or list(cfg.agents)
    for a in agents:
        cfg.get(a)  # fail fast on unknown names
    project = Project("smoke", proj_dir, media_dir)
    results = {}

    def run(agent: str):
        token = f"PT-{agent}-{secrets.token_hex(4)}"
        out = proj_dir / "smoke_out" / f"{agent}.txt"
        out.unlink(missing_ok=True)
        prompt = (
            "# Smoke test\n\n"
            "You are being tested by an automation harness. Do exactly this and nothing else:\n\n"
            f"1. Read the file {(media_dir / 'media_token.txt').as_posix()} (it contains one line).\n"
            f"2. Create the file {out.as_posix()} (absolute path; create the folder if needed).\n"
            "3. Write exactly two lines into it:\n"
            "   - line 1: the line you read in step 1, or MEDIA-UNREADABLE if you could not read it\n"
            "   - line 2: the PROMPT TOKEN given on the last line of this message\n\n"
            "Do not ask questions. Do not modify any other file. Then stop.\n\n"
            f"PROMPT TOKEN: {token}\n"
        )
        defn = cfg.agents[agent]
        defn.timeout = args.timeout
        runner = lo.Runner(project, "smoke", False, cfg, agent)
        t0 = time.time()
        r = {"agent": agent, "prompt_mode": defn.prompt_mode}
        try:
            res = runner.run_agent(prompt)
            r["exit"] = res.returncode
            r["stdout_tail"] = (res.stdout or "").strip()[-400:]
            r["stderr_tail"] = (res.stderr or "").strip()[-400:]
        except LoopError as e:
            r["exit"] = f"EXC: {e}"
        r["seconds"] = round(time.time() - t0, 1)
        lines = out.read_text(encoding="utf-8", errors="replace").splitlines() if out.exists() else []
        r["file_written"] = out.exists()
        r["prompt_intact"] = len(lines) > 1 and lines[1].strip() == token
        r["media_read"] = len(lines) > 0 and lines[0].strip() == media_token
        r["passed"] = r["exit"] == 0 and r["prompt_intact"] and r["media_read"]
        results[agent] = r

    threads = [threading.Thread(target=run, args=(a,)) for a in agents]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    (work / "smoke_results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n{'AGENT':<10} {'MODE':<8} {'EXIT':<6} {'SEC':<7} {'WRITTEN':<8} {'PROMPT_INTACT':<14} {'MEDIA_READ':<11} RESULT")
    for a in agents:
        r = results[a]
        print(f"{a:<10} {r['prompt_mode']:<8} {str(r['exit'])[:5]:<6} {r['seconds']:<7} {str(r['file_written']):<8} "
              f"{str(r['prompt_intact']):<14} {str(r['media_read']):<11} {'PASS' if r['passed'] else 'FAIL'}")
    print(f"\nDetails (stdout/stderr tails): {work / 'smoke_results.json'}")
    print(f"Full agent output: {proj_dir / '.loop' / 'runs'}")
    return 0 if all(r["passed"] for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
