"""Run the installed package directly, including from a different working directory."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.cli import main

raise SystemExit(main())
