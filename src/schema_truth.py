from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

from .persistence import load_json_file


SchemaTruth = Dict[str, Dict]


def load_schema_truth(path: Path) -> SchemaTruth:
	data = load_json_file(path, default={})
	# Normalize synonyms to lowercase for matching
	for key, meta in data.items():
		syn = meta.get("synonyms", [])
		meta["_syn_lc"] = [s.strip().lower() for s in syn + [meta.get("header", key)]]
		pattern = meta.get("header_regex")
		if pattern:
			try:
				meta["_header_re"] = re.compile(pattern)
			except re.error:
				meta["_header_re"] = None
	return data


def canonical_keys(truth: SchemaTruth) -> List[str]:
	return list(truth.keys())
