"""Optional stdio connector; importing it never starts the server."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.mcp_server import main

if __name__ == "__main__":
    raise SystemExit(main())
