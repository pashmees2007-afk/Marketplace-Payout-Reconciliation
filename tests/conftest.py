import sys
from pathlib import Path

# Make the project root importable (reconcile, agent, report, money, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
