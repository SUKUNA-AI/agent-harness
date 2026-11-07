"""Pinned subprocess bridge, copied into a local normalized asset bundle.

Usage: bridge.py PACKAGE_SRC RECEIPT GRADE_INPUT DEADLINE RECEIPT_SHA256.
PACKAGE_SRC is supplied by reviewed controller code, never by task output.
The versioned request selects legacy unverified output or the v2 execution
chain; the bridge never fills candidate fields from a trial report.
"""
import sys

if __name__ == "__main__":
    sys.path.insert(0, sys.argv[1])
    from agent_harness.benchmark_bundles import evaluator_main
    evaluator_main(sys.argv[2:])
