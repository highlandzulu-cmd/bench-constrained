"""bench list | gen-tasks | manifest | preflight | run | selftest | summary | clean"""
from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from pathlib import Path

import yaml

from . import report
from .config import ROOT, load_profiles, load_settings
from .manifests import build_job, build_translator, guaranteed_qos
from .preflight import assess, scope_problem, worst_case, worst_case_disk_mib
from .runner import make_cells, run_cell, run_matrix
from .tasks import generate_tasks, get_task, load_tasks, write_tasks_yaml


def _run_id() -> str:
    return time.strftime("%m%d-%H%M%S")


def _select(tasks, names):
    return tasks if not names else [get_task(tasks, n) for n in names.split(",")]


def cmd_list(a) -> int:
    for t in load_tasks():
        print(f"{t.id:12} {t.fixture:16} {t.timeout_s // 60:3} min  verify: {t.verify_cmd}")
    return 0


def cmd_gen_tasks(a) -> int:
    path = write_tasks_yaml(generate_tasks(Path(a.src)))
    print(f"wrote {path} ({len(load_tasks())} tasks)")
    return 0


def cmd_manifest(a) -> int:
    s, profiles = load_settings(), load_profiles()
    if a.synth:
        job = build_job(run_id="manual", task_id="SYNTH", profile=profiles[a.profile], repeat=1, settings=s,
                        timeout_s=60, mode="verify", synth=a.synth, deadline_s=a.deadline)
    else:
        t = get_task(load_tasks(), a.task)
        job = build_job(run_id="manual", task_id=t.id, profile=profiles[a.profile], repeat=1, settings=s,
                        timeout_s=t.timeout_s, mode=a.mode)
    assert guaranteed_qos(job)
    print(yaml.safe_dump(job, sort_keys=False))
    return 0


def cmd_translator_manifest(a) -> int:
    s = load_settings()
    if not _require_scope(s):
        return 2
    print(yaml.safe_dump_all(build_translator(s), sort_keys=False))
    return 0


def _cluster(a, s):
    from .k8s import Cluster
    return Cluster(s.namespace, kubeconfig=a.kubeconfig, context=a.context)


def _require_scope(s) -> bool:
    problem = scope_problem(s)
    if problem:
        print(f"refusing to run: {problem}", file=sys.stderr)
        return False
    return True


def cmd_preflight(a) -> int:
    """Read-only: where would pods go, who is already there, how big is our worst case."""
    s, profiles = load_settings(), load_profiles()
    if not _require_scope(s):
        return 2
    chosen = [profiles[n] for n in a.profile.split(",")]
    if a.total_parallel:                      # several runs at once: size the check for all of them together
        s = dataclasses.replace(s, max_parallel=a.total_parallel)
    cpu, mem = worst_case(s, chosen)
    cluster = _cluster(a, s)
    print(f"namespace: {s.namespace} (the only one this tool touches)")
    print(f"pods may run ONLY on: {', '.join(s.allowed_nodes) or s.node_selector}")
    prio = s.priority_class
    if prio:
        value = cluster.priority_class_value(prio)
        print(f"priority class {prio}: " + ("MISSING in the cluster" if value is None else f"value {value} (lower than other teams' pods means they can preempt ours)"))
    print(f"at most {s.max_parallel} pods at once; worst case if all land on one node: {cpu:g} CPU, {mem / 1024:g} GiB, {worst_case_disk_mib(s) / 1024:g} GiB scratch disk\n")
    stop = False
    for use in cluster.inspect_nodes(s.allowed_nodes):
        print(use.name)
        for level, msg in assess(use, cpu, mem, worst_case_disk_mib(s)):
            print(f"  [{level}] {msg}")
            stop |= level == "stop"
    print("\nverdict:", "DO NOT RUN, see [stop] above" if stop else "nothing here stops a run; check the [warn] lines")
    return 1 if stop else 0


def cmd_run(a) -> int:
    s, profiles = load_settings(), load_profiles()
    if not _require_scope(s):
        return 2
    if a.harness:
        from .harnesses import BUILDERS
        if a.harness not in BUILDERS:
            print(f"unknown harness {a.harness!r}; known: {sorted(BUILDERS)}", file=sys.stderr)
            return 2
        s = dataclasses.replace(s, agent={**s.agent, "harness": a.harness})
    harness = s.agent.get("harness", "loop")
    names = a.profile.split(",")
    chosen = [profiles[n] for n in names]
    tasks = _select(load_tasks(), a.tasks)
    run_id = a.run_id or _run_id()
    cells = make_cells(run_id, tasks, chosen, a.repeats or s.repeats, a.mode)
    for c in cells:
        c["harness"] = harness
    results_dir = ROOT / s.results_dir
    print(f"harness: {harness}")
    print(f"run {run_id}: {len(cells)} cells ({len(tasks)} tasks x {len(chosen)} profiles x "
          f"{a.repeats or s.repeats} repeats), up to {a.parallel or s.max_parallel} at a time, mode={a.mode}")
    cluster = _cluster(a, s)
    done = []

    def show(rec):
        done.append(rec)
        print(f"[{len(done)}/{len(cells)}] {rec['task_id']:12} {rec['profile']:11} r{rec['repeat']} "
              f"-> {rec['status']:11} peak={rec.get('peak_mem_mib')} MiB wall={rec.get('wall_clock_s')}s")
        if rec["status"] in ("infra_error", "fail", "timeout") and rec.get("notes"):
            print("      why: " + " | ".join(str(n) for n in rec["notes"])[:400].replace("\n", " "))
        if rec["status"] == "fail" and rec.get("agent_exit_code") not in (0, None):
            last = (rec.get("agent_tail") or "").strip().splitlines()[-3:]
            print(f"      the agent exited with code {rec.get('agent_exit_code')}; its last lines: " + " | ".join(last)[:400])

    code_cm = None
    if a.code_from_configmap:
        code_cm = f"bench-code-{run_id}".lower()[:63]
        cluster.create_configmap(code_cm, _code_files(), run_id)
        print(f"runner code mounted from ConfigMap {code_cm} over the image's copy (this headnode's files, no rebuild)")
    try:
        recs = run_matrix(cells, profiles, s, cluster, results_dir, max_parallel=a.parallel, keep=a.keep,
                          on_done=show, code_configmap=code_cm)
    finally:
        if code_cm:
            cluster.delete_configmap(code_cm)
    print("\n" + report.markdown(report.summarize(recs)))
    print(f"\nresults: {results_dir / run_id}")
    return 0


# Checks that the limits and the measurements are real (run before trusting any result).
SELFTEST = [
    ("alloc 1 GiB under 512 MiB is an OOM", "alloc:1024", "tiny", "oom", None),
    ("a child process OOM-killed inside, parent survives", "alloc_child:1024", "tiny", "oom", "inside"),
    ("a sleep past the deadline is a timeout", "sleep:600", "tiny", "timeout", None),
    ("a small allocation under the limit is fine", "alloc:64", "tiny", "pass", None),
]


def _code_files() -> dict[str, str]:
    """The runner's own source, as ConfigMap data (flat file names)."""
    return {p.name: p.read_text() for p in sorted((ROOT / "bench").glob("*.py")) if p.name != "cli.py"}


def cmd_selftest(a) -> int:
    s, profiles = load_settings(), load_profiles()
    if not _require_scope(s):
        return 2
    cluster = _cluster(a, s)
    results_dir = ROOT / s.results_dir
    run_id = a.run_id or f"selftest-{_run_id()}"
    image, code_cm = None, None
    if a.stock_image:
        image, code_cm = a.stock_image, f"bench-code-{run_id}".lower()[:63]
        cluster.create_configmap(code_cm, _code_files(), run_id)
        print(f"using stock image {image} with the runner mounted from ConfigMap {code_cm} (no custom image)")
    try:
        return _selftest_cells(a, s, profiles, cluster, results_dir, run_id, image, code_cm)
    finally:
        if code_cm:
            cluster.delete_configmap(code_cm)


def _selftest_cells(a, s, profiles, cluster, results_dir, run_id, image, code_cm) -> int:
    ok_all = True
    for title, synth, prof, expect, extra in ([] if a.only == "cpu" else SELFTEST):
        cell = {"run_id": run_id, "task_id": f"SYNTH:{synth}", "profile": prof, "repeat": 1, "mode": "verify",
                "backend": "k8s", "arch": profiles[prof].arch, "cpu_limit": profiles[prof].cpu_limit,
                "mem_limit_mib": profiles[prof].memory_mib, "emulated": False, "timeout_s": 60}
        deadline = 45 if expect == "timeout" else None
        rec = run_cell(cell, profiles[prof], s, cluster, results_dir, synth=synth, deadline_s=deadline,
                       image=image, code_configmap=code_cm)
        good = rec["status"] == expect and (extra != "inside" or rec.get("oom_kills_inside", 0) >= 1)
        ok_all &= good
        print(f"{'PASS' if good else 'FAIL'}  {title}: status={rec['status']} inside={rec.get('oom_kills_inside')} {rec.get('notes')}")
    # CPU: 4 busy processes under the 1 CPU limit must get about 1 CPU of time in total, not 4.
    cell = {"run_id": run_id, "task_id": "SYNTH:cpuburn:4:8", "profile": "tiny", "repeat": 1, "mode": "verify",
            "backend": "k8s", "arch": "any", "cpu_limit": profiles["tiny"].cpu_limit,
            "mem_limit_mib": profiles["tiny"].memory_mib, "emulated": False, "timeout_s": 120}
    rec = run_cell(cell, profiles["tiny"], s, cluster, results_dir, synth="cpuburn:4:8", image=image,
                   code_configmap=code_cm)
    used, wall = rec.get("cpu_usage_s") or 0, rec.get("wall_clock_s") or 0
    share = used / wall if wall else 0
    good = rec["status"] == "pass" and 0.8 <= share <= 1.2
    ok_all &= good
    print(f"{'PASS' if good else 'FAIL'}  4 busy processes under a 1 CPU limit used {used:g} CPU-s in {wall:g}s "
          f"= {share:.2f} CPUs (4 would be 4.00), throttled {rec.get('cpu_throttled_pct')}% of periods")
    return 0 if ok_all else 1


def cmd_exec(a) -> int:
    """Run one shell command inside a real benchmark pod (same limits, nodes and user) and print what it said."""
    s, profiles = load_settings(), load_profiles()
    if not _require_scope(s):
        return 2
    cluster = _cluster(a, s)
    run_id = f"exec-{_run_id()}"
    code_cm = None
    if not a.image_code:
        code_cm = f"bench-code-{run_id}".lower()[:63]
        cluster.create_configmap(code_cm, _code_files(), run_id)
    p = profiles[a.profile]
    cell = {"run_id": run_id, "task_id": "EXEC", "profile": p.name, "repeat": 1, "mode": "verify", "backend": "k8s",
            "arch": p.arch, "cpu_limit": p.cpu_limit, "mem_limit_mib": p.memory_mib, "emulated": False, "timeout_s": 180}
    try:
        rec = run_cell(cell, p, s, cluster, ROOT / s.results_dir, deadline_s=240, code_configmap=code_cm, exec_cmd=a.command)
    finally:
        if code_cm:
            cluster.delete_configmap(code_cm)
    print(rec.get("exec_output") or f"(no output) status={rec['status']} notes={rec.get('notes')}")
    print(f"--- exit code {rec.get('exec_exit_code')}, peak {rec.get('peak_mem_mib')} MiB, node {rec.get('node')}")
    return 0


def cmd_summary(a) -> int:
    s = load_settings()
    recs = report.load_results(ROOT / s.results_dir, a.run_id)
    print(report.markdown(report.summarize(recs)))
    return 0


def cmd_clean(a) -> int:
    s = load_settings()
    n = _cluster(a, s).clean(a.run_id)
    print(f"deleted {n} jobs labelled app=bench-constrained in namespace {s.namespace}")
    return 0


def main(argv=None) -> int:
    # Show each line as it happens even when output goes to a file (nohup ... > run.log), instead of in one late chunk.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(prog="bench")
    p.add_argument("--kubeconfig")
    p.add_argument("--context")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    g = sub.add_parser("gen-tasks"); g.add_argument("--src", required=True, help="folder holding tasks/suite.yaml"); g.set_defaults(fn=cmd_gen_tasks)
    m = sub.add_parser("manifest"); m.add_argument("--task", default="BUG-01"); m.add_argument("--profile", default="tiny")
    m.add_argument("--mode", default="agent", choices=["agent", "verify"]); m.add_argument("--synth", default="")
    m.add_argument("--deadline", type=int); m.set_defaults(fn=cmd_manifest)
    r = sub.add_parser("run"); r.add_argument("--profile", default="tiny", help="comma separated")
    r.add_argument("--tasks", default="", help="comma separated ids; default all 13")
    r.add_argument("--repeats", type=int); r.add_argument("--parallel", type=int); r.add_argument("--run-id")
    r.add_argument("--mode", default="agent", choices=["agent", "verify"])
    r.add_argument("--harness", help="loop, dsh, codex, pi, goose, cline, kilo, omp, opencode or hermes (default: agent.harness in settings.yaml)")
    r.add_argument("--keep", action="store_true", help="keep the Jobs after collecting results")
    r.add_argument("--code-from-configmap", action="store_true",
                   help="run this folder's runner code inside the image's pods (no image rebuild after a code change)")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("translator-manifest", help="print the Codex translator service definition (apply with kubectl)").set_defaults(fn=cmd_translator_manifest)
    pf = sub.add_parser("preflight"); pf.add_argument("--profile", default="tiny", help="comma separated; sizes the worst case")
    pf.add_argument("--total-parallel", type=int, help="pods you plan to run at once across ALL concurrent runs")
    pf.set_defaults(fn=cmd_preflight)
    t = sub.add_parser("selftest"); t.add_argument("--run-id")
    t.add_argument("--stock-image", nargs="?", const="python:3.12-slim", default=None, metavar="IMAGE",
                   help="run on a public image with the runner mounted from a ConfigMap (no custom image needed)")
    t.add_argument("--only", choices=["cpu"], help="run just the CPU check")
    t.set_defaults(fn=cmd_selftest)
    ex = sub.add_parser("exec", help="run one shell command inside a benchmark pod and show its output (debugging)")
    ex.add_argument("command"); ex.add_argument("--profile", default="tiny")
    ex.add_argument("--image-code", action="store_true", help="use the runner code baked into the image, not this folder's")
    ex.set_defaults(fn=cmd_exec)
    su = sub.add_parser("summary"); su.add_argument("run_id"); su.set_defaults(fn=cmd_summary)
    c = sub.add_parser("clean"); c.add_argument("--run-id"); c.set_defaults(fn=cmd_clean)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
