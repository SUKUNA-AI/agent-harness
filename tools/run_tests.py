"""Run the source-checkout suite without a global install or third-party deps."""
import argparse
import hashlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def test_cases(suite):
    for entry in suite:
        if isinstance(entry, unittest.TestSuite):
            yield from test_cases(entry)
        else:
            yield entry


def shard_number(test_id, count):
    digest = hashlib.sha256(test_id.encode("utf-8")).digest()
    return int.from_bytes(digest, "big") % count + 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, default=1, help="One-based shard index")
    parser.add_argument("--shards", type=int, default=1, help="Number of disjoint shards")
    args = parser.parse_args()
    if not 1 <= args.shard <= args.shards:
        parser.error("require 1 <= --shard <= --shards")

    sys.path.insert(0, str(ROOT / "src"))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    if args.shards > 1:
        total = suite.countTestCases()
        suite = unittest.TestSuite(
            case for case in test_cases(suite)
            if shard_number(case.id(), args.shards) == args.shard
        )
        print(
            f"Shard {args.shard}/{args.shards}: {suite.countTestCases()} of {total} discovered tests",
            flush=True,
        )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and result.testsRun > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
