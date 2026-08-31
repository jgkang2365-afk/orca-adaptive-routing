#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adaptive_coordinator.benchmark import benchmark_violations, run_benchmark

results = run_benchmark()
print(json.dumps(results, indent=2, sort_keys=True))
violations = benchmark_violations(results)
if violations:
    print("benchmark invariant failure: " + ", ".join(violations), file=sys.stderr)
    raise SystemExit(1)
