"""Record what the old alarm rules answer for alarm_cases.CASES (run by hand, once):

    .venv/bin/python plugins/heinzel/tests/record_alarm_fixtures.py OLD_TOOLS_DIRECTORY

Imports the old module's pure functions only; reads no network and writes only tests/fixtures/alarm/rules.json."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, sys.argv[1])
import alarm_cases  # noqa: E402
import hz_alarm  # noqa: E402

out = [{"case": n, "function": name, "result": alarm_cases.call(hz_alarm, name, args, kwargs)} for n, (name, args, kwargs) in enumerate(alarm_cases.CASES)]
path = Path(__file__).parent / "fixtures" / "alarm" / "rules.json"
path.write_text(json.dumps(out, indent=1) + "\n")
print(f"wrote {path}: {len(out)} cases")
