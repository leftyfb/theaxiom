"""Opt-in wall-clock timings for the real Chromium smoke scripts."""
import os
from time import perf_counter


def timed_case(scenario, viewport, run, *args):
    if os.environ.get("AXIOM_PROFILE_BROWSER") != "1":
        return run(*args)
    start = perf_counter()
    outcome = "failed"
    try:
        result = run(*args)
        outcome = "passed"
        return result
    finally:
        print(f"BROWSER_TIMING {scenario} {viewport} {perf_counter() - start:.3f}s {outcome}", flush=True)
