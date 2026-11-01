"""Required real-Pyright integration smoke; missing server is failure, not skip."""
from pathlib import Path
import argparse
import json
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=ROOT / ".test-tmp" / "language-tools")
    args = parser.parse_args()
    # Keep the npm wrapper path: Windows must exercise the shell-free resolver.
    executable = args.prefix.absolute() / "node_modules" / ".bin" / ("pyright-langserver.cmd" if os.name == "nt" else "pyright-langserver")
    if not executable.is_file():
        parser.error("Install the pinned local fixture with npm install --prefix <prefix> --ignore-scripts pyright@1.1.414")
    os.environ["HARNESS_TEST_LSP_SERVER"] = json.dumps([str(executable), "--stdio"])
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName("test_lsp.RealLSPTests"))
    if not result.wasSuccessful() or result.testsRun != 1 or result.skipped:
        return 1
    print(json.dumps({"status": "passed", "server": "pyright", "source": "explicit local npm installation",
                      "checks": ["cross-file references", "rename preview", "apply", "definition after rename"],
                      "tests": result.testsRun, "skipped": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
