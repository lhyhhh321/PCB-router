# Router acceleration benchmarks

Run comparisons serially from the repository root, with no other heavy work:

```bash
python -m pip install -r requirements-torch.txt
python -m benchmarks.compare_backends \
  'pcb_example/03第三层右侧删去线网（布线输入）.json' \
  --backends python torch-cpu torch-mps --output benchmarks/file03-backends.json
```

Use `torch-cuda` instead of `torch-mps` on an NVIDIA machine. The comparison
uses all default routing stages and settings, checks exact emitted wires and
completion/conflict results, and fails if any backend differs. Output includes
a SHA-256 fingerprint of that comparison plus the input board fingerprint.
PyTorch import and a one-element device warm-up happen before the timed runs;
router construction, tensor fields, transfers and final checks are timed.
CLI startup can therefore take longer. Each backend gets a fresh router.

For a quick check, add `--initial-jobs 40 --repeat 3`. This intentionally
cancels before repair/legalization and is **not** a finished board benchmark.
For profiling, run `--backends python --initial-jobs 40 --profile /tmp/router.prof`.
Profiled times include instrumentation overhead and should not be compared
with unprofiled times.

The original 40-job profile spent 3.35 of 5.62 seconds in `_dynamic_context`
(98,259 calls), versus 0.50 seconds in `_static_legal`. This motivated batching
congestion evaluation rather than rewriting the A* search or force algorithm.
Independent random-order trials are another possible process-parallel unit,
but their early stopping, cancellation and live preview need coordination.
They remain sequential in this implementation; within a trial, routing order
is part of the algorithm and cannot simply be parallelized without changing
its behavior.

## Measured full-board result

One complete run per backend on an Apple M5, macOS 27.2, Python 3.14.6,
PyTorch 2.14.1, using file 03 and every default routing stage:

| Backend | Seconds | Speedup versus Python |
| --- | ---: | ---: |
| `python` | 724.927 | 1.00× |
| `torch-cpu` | 416.191 | 1.74× |
| `torch-mps` | 433.657 | 1.67× |

All three runs emitted **exactly the same wires**, routed **204/220** requests,
reported **zero conflict cells**, and emitted **32,723.182** source units of
copper. Both tensor backends built 2,680 fields with no budget/numerical
fallbacks. The [raw results](file03-backends.json) include matching output
fingerprints. Timings are individual measurements, not statistical averages.

PyTorch CPU was fastest on this board and machine. MPS still improved on
Python but did not beat the batched CPU implementation overall. GPU launches,
packing and transfers add overhead; this benchmark does not isolate their
individual costs. Device availability alone is not a speed prediction. CUDA was not available on this machine and remains untested
on hardware. Most of A*, geometric checks and route refinement remain serial,
so the speedup is limited even though the congestion fields run in parallel.
