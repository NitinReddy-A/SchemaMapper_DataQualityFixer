from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def load_json_file(path: Path, default: Any = None) -> Any:
	if not path.exists():
		return default
	with path.open("r", encoding="utf-8") as f:
		return json.load(f)


def save_json_file(path: Path, data: Any) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("w", encoding="utf-8") as f:
		json.dump(data, f, ensure_ascii=False, indent=2)
