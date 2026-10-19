"""Export one local event's metadata read-only; never initializes or replays a runtime."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infrastructure.execution_report import export_timeline


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--event-key', required=True)
    args = parser.parse_args()
    print(json.dumps(export_timeline(args.database, args.event_key), ensure_ascii=False, indent=2))
