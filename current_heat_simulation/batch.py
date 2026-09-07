"""Independent layout/day processes and content-checked atomic checkpoints."""

from concurrent.futures import ProcessPoolExecutor, as_completed, wait, FIRST_COMPLETED
from contextlib import nullcontext
import hashlib
import json
import multiprocessing
from pathlib import Path
import tempfile
from time import perf_counter

from tqdm import tqdm

from .astar_planner import WarehouseMap
from .run_metrics import write_json
from .warehouse_system import WarehouseSystem

REQUIRED_FILES = ("summary.json", "tasks.csv", "rack_jobs.csv", "robots.csv",
                  "workstations.csv", "blocking_hotspots.csv")
_SHARED = None


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def code_fingerprint():
    root = Path(__file__).resolve().parent.parent
    return fingerprint({str(path.relative_to(root)): file_hash(path)
                        for package in ("current_heat_simulation", "amr_simulation", "warehouse_layout")
                        for path in sorted((root/package).glob("*.py"))})


def read_checkpoint(directory, expected):
    try:
        checkpoint = json.loads((directory/"checkpoint.json").read_text())
        if checkpoint["fingerprint"] != expected:
            return None
        for name in REQUIRED_FILES:
            if file_hash(directory/name) != checkpoint["files"][name]:
                return None
        summary = json.loads((directory/"summary.json").read_text())
        return summary if summary.get("success_flag") and summary.get("status") == "completed" else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _init_worker(shared):
    global _SHARED
    _SHARED = shared


def run_day(shared, name, day, output):
    """No mutable simulator state escapes a day. Publish checkpoint last via rename."""
    started = perf_counter()
    directory = Path(output)/name/day
    directory.parent.mkdir(parents=True, exist_ok=True)
    key = fingerprint({"batch": shared["fingerprint"], "layout": name, "date": day})
    with tempfile.TemporaryDirectory(prefix=f".{day}-", dir=directory.parent) as scratch:
        staged = Path(scratch)/"day"
        staged.mkdir()
        system = None
        try:
            system = WarehouseSystem(
                shared["config"], WarehouseMap(shared["grid"]), shared["days"][day],
                shared["layouts"][name]["racks"], shared["mapping"], shared["amr"],
            )
            progress_queue = shared.get("progress_queue")
            if progress_queue is not None:
                system.progress_callback = lambda lines: progress_queue.put((name, int(lines)))
                system.status_callback = lambda seconds: progress_queue.put((name, "time", float(seconds)))
            summary = system.run(headless=shared["headless"], output=staged,
                                 max_seconds=shared["max_seconds"],
                                 diagnostic_path=directory.parent/f"{day}.diagnostics.jsonl")
        except Exception as exc:
            if system is not None and system.run_summary is not None:
                summary = system.run_summary
            else:
                tasks = shared["days"][day]
                summary = {"date": day, "method": "current_heat", "status": "failed",
                           "success_flag": False, "failure_reason": f"{type(exc).__name__}: {exc}",
                           "source_lines": sum(t.source_lines for t in tasks), "completed_lines": 0,
                           "unfinished_lines": sum(t.source_lines for t in tasks), "completion_ratio": 0,
                           "sim_duration_s": 0, "line_throughput_per_hour": None}
        summary.update(layout=name, slotting_strategy=shared["layouts"][name].get("strategy"),
                       run_wall_clock_seconds=perf_counter()-started)
        write_json(staged/"summary.json", summary)
        if summary["success_flag"]:
            write_json(staged/"checkpoint.json", {
                "fingerprint": key, "files": {name: file_hash(staged/name) for name in REQUIRED_FILES},
            })
        # Preserve the last published directory until replacement is ready.
        backup = Path(scratch)/"previous"
        if directory.exists():
            directory.replace(backup)
        try:
            staged.replace(directory)
        except BaseException:
            if backup.exists():
                backup.replace(directory)
            raise
    return summary


def _worker(name, day, output):
    return run_day(_SHARED, name, day, output)


def execute_batch(shared, output, workers, resume=False):
    started = perf_counter()
    results, pending = [], []
    bars = {}
    worker_count = min(workers, len(shared["layouts"]) * len(shared["days"]))
    manager = multiprocessing.Manager()
    progress_queue = manager.Queue()
    shared["progress_queue"] = progress_queue

    def drain_progress():
        while True:
            try:
                item = progress_queue.get_nowait()
            except Exception:
                return
            if len(item) == 3 and item[1] == "time":
                name, _, seconds = item
                bars[name].set_postfix_str(f"sim {seconds:.0f}s", refresh=True)
            else:
                name, lines = item
                bars[name].update(int(lines))

    for day in sorted(shared["days"]):
        for name in shared["layouts"]:
            expected = fingerprint({"batch": shared["fingerprint"], "layout": name, "date": day})
            checkpoint = read_checkpoint(output/name/day, expected) if resume else None
            if checkpoint is not None:
                results.append(checkpoint)
                print(f"{name} {day}: resumed completed day", flush=True)
            else:
                pending.append((name, day))
    worker_count = min(workers, len(shared["layouts"]), len(pending)) if pending else 0

    if worker_count <= 1:
        class LocalProgress:
            def put(self, item):
                if len(item) == 3 and item[1] == "time":
                    name, _, seconds = item
                    bars[name].set_postfix_str(f"sim {seconds:.0f}s", refresh=True)
                else:
                    name, lines = item
                    bars[name].update(int(lines))
            def get_nowait(self):
                raise Exception("empty")
        shared["progress_queue"] = LocalProgress()

    total = len(pending)+len(results)

    def collect(summary):
        results.append(summary)
        tqdm.write(f"[{len(results)}/{total}] "
              f"{summary['layout']} {summary['date']}: {summary['status']}, "
              f"{summary.get('completed_lines', 0)} lines")

    try:
        pool_context = (ProcessPoolExecutor(max_workers=worker_count,
                        mp_context=multiprocessing.get_context("spawn"),
                        initializer=_init_worker, initargs=(shared,)) if worker_count > 1 else nullcontext())
        with pool_context as pool:
            # A date is a barrier: collect every layout before submitting the next.
            for day in sorted(shared["days"]):
                print(f"Running layouts for {day}", flush=True)
                day_total = sum(task.source_lines for task in shared["days"][day])
                resumed = {r["layout"]: int(r.get("completed_lines", 0)) for r in results if r["date"] == day}
                bars = {name: tqdm(total=day_total, initial=resumed.get(name, 0),
                                   desc=f"{day} {name}", unit=" lines", position=index, leave=True)
                        for index, name in enumerate(shared["layouts"])}
                try:
                    if pool is None:
                        for name, run_day_date in pending:
                            if run_day_date == day:
                                collect(run_day(shared, name, day, output))
                        continue
                    futures = {pool.submit(_worker, name, day, output): (name, day) for name, run_day_date in pending if run_day_date == day}
                    pending_futures = set(futures)
                    while pending_futures:
                        done, pending_futures = wait(pending_futures, timeout=0.25,
                                                     return_when=FIRST_COMPLETED)
                        drain_progress()
                        for future in done:
                            try:
                                collect(future.result())
                            except Exception as exc:
                                name, failed_day = futures[future]
                                collect({"layout": name, "date": failed_day, "status": "failed", "success_flag": False,
                                         "failure_reason": f"worker failure: {type(exc).__name__}: {exc}"})
                        drain_progress()
                finally:
                    drain_progress()
                    for row in results:
                        if row["date"] == day:
                            bar = bars[row["layout"]]
                            remaining = int(row.get("completed_lines", 0)) - bar.n
                            if remaining > 0:
                                bar.update(remaining)
                    for bar in reversed(list(bars.values())):
                        bar.close()
                    # Leave the cursor below every completed layout bar.
                    print("\n" * (len(bars)-1), end="", flush=True)
                    bars = {}
    finally:
        manager.shutdown()
    timing = {"workers": worker_count, "scheduled_runs": len(pending),
              "resumed_runs": len(results)-len(pending), "wall_clock_seconds": perf_counter()-started}
    write_json(output/"batch_timing.json", timing)
    return sorted(results, key=lambda r: (r["layout"], r["date"]))
