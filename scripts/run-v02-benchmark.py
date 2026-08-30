#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adaptive_coordinator.benchmark import run_benchmark

print(json.dumps(run_benchmark(), indent=2, sort_keys=True))
