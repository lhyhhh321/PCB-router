"""Reproducible serial backend comparison, with exact output parity checks.

Run from the repository root: python -m benchmarks.compare_backends BOARD.json
Use --initial-jobs 40 for a quick, intentionally cancelled prefix benchmark.
"""
import argparse
import cProfile
import json
import hashlib
from pathlib import Path
import platform
import time

from pcb_viewer.router import Autorouter, RouterConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('board', type=Path)
    parser.add_argument('--backends', nargs='+', default=['python', 'torch-cpu', 'torch-mps'])
    parser.add_argument('--initial-jobs', type=int)
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile', type=Path, help='Optional cProfile output for the first baseline run')
    args = parser.parse_args()
    if args.repeat < 1 or (args.initial_jobs is not None and args.initial_jobs < 1):
        parser.error('repeat and initial-jobs must be positive')
    data = json.loads(args.board.read_text())
    data['wires'] = [wire for wire in data.get('wires', []) if wire.get('route_type') != 'autoroute']
    results = []
    reference = None
    # Import and initialize the runtime outside timed routing for warm comparisons.
    # End-user CLI elapsed time still includes import/device startup.
    if any(backend.startswith('torch-') for backend in args.backends):
        from pcb_viewer.torch_costs import TorchCosts
        for backend in args.backends:
            if backend.startswith('torch-'):
                engine = TorchCosts(backend.removeprefix('torch-'))
                engine.torch.ones(1, device=engine.device).cpu()
    for repeat in range(args.repeat):
        for backend in args.backends:
            config = RouterConfig(compute_backend=backend)
            last_notice = time.monotonic()
            def progress(done, total, message):
                nonlocal last_notice
                if time.monotonic() - last_notice > 30:
                    print(f'{backend}: {done}/{total} {message}', flush=True)
                    last_notice = time.monotonic()
                return args.initial_jobs is None or done < args.initial_jobs
            profiler = cProfile.Profile() if args.profile and not results else None
            if profiler:
                profiler.enable()
            started = time.perf_counter()
            router = Autorouter(data, config)
            wires, report = router.route(progress)
            elapsed = time.perf_counter() - started
            if profiler:
                profiler.disable()
                profiler.dump_stats(str(args.profile))
            comparison = (wires, report.routed, report.failed, report.conflict_cells,
                          report.conflicting_jobs, report.wire_length)
            if reference is None:
                reference = comparison
            parity = comparison == reference
            result = dict(backend=backend, repeat=repeat + 1, seconds=round(elapsed, 3),
                          routed=report.routed, conflict_cells=report.conflict_cells,
                          wire_length=report.wire_length, exact_parity=parity,
                          output_sha256=hashlib.sha256(json.dumps(comparison, sort_keys=True).encode()).hexdigest(),
                          fields_built=router.torch_costs.fields_built if router.torch_costs else 0,
                          fields_skipped=router.torch_costs.fields_skipped if router.torch_costs else 0)
            results.append(result)
            print(json.dumps(result), flush=True)
            if args.output:
                args.output.write_text(json.dumps(dict(platform=platform.platform(),
                    python=platform.python_version(), initial_jobs=args.initial_jobs,
                    board_sha256=hashlib.sha256(args.board.read_bytes()).hexdigest(),
                    torch=engine.torch.__version__ if any(b.startswith("torch-") for b in args.backends) else None,
                    config=report.config, results=results), indent=2) + '\n')
            if not parity:
                raise SystemExit('Backend changed the routing result')


if __name__ == '__main__':
    main()
