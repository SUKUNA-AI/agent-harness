"""Запускает native-session adapter непосредственно из переносимого пакета."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.native_session import main

raise SystemExit(main())
