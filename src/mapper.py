from __future__ import annotations

import re
from typing import Dict, List, Tuple

from rapidfuzz.distance.Levenshtein import normalized_similarity

from .schema_truth import SchemaTruth
from .llm import map_headers_with_llm


SEPARATORS_RE = re.compile(r"[\s_\-./]+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]")


def _normalize_header(text: str) -> str:
	if text is None:
		return ""
	lc = text.strip().lower()
	lc = re.sub(r"\s+", " ", lc)
	# Preserve semantic hints for symbols before stripping others
	lc = lc.replace("%", " pct ")
	lc = lc.replace("#", " num ")
	# Compact form for similarity
	compact = NON_ALNUM_RE.sub("", lc)
	return compact


def suggest_mapping(
	source_headers: List[str],
	truth: SchemaTruth,
	learned_synonyms: Dict[str, List[str]] | None = None,
	use_llm: bool = False,
) -> Tuple[Dict[str, Dict], List[str]]:
	learned_synonyms = learned_synonyms or {}
	result: Dict[str, Dict] = {}
	unmatched: List[str] = []

	# Build lookup of normalized synonym -> canonical
	syn_to_canon: Dict[str, str] = {}
	for canon, meta in truth.items():
		for s in meta.get("_syn_lc", []):
			syn_to_canon[_normalize_header(s)] = canon
		# learned/prompted synonyms
		for s in learned_synonyms.get(canon, []):
			syn_to_canon[_normalize_header(s)] = canon

	for src in source_headers:
		norm = _normalize_header(src)

		# 1) Exact canonical key match
		if norm in (_normalize_header(k) for k in truth.keys()):
			for k in truth.keys():
				if _normalize_header(k) == norm:
					result[src] = {"canonical": k, "confidence": 1.00, "method": "canonical"}
					break
			continue

		# 2) Regex header match (prefer precise pattern cues like % vs id)
		regex_hit = None
		for canon, meta in truth.items():
			re_obj = meta.get("_header_re")
			if re_obj and re_obj.match(src):
				regex_hit = canon
				break
		if regex_hit:
			result[src] = {"canonical": regex_hit, "confidence": 0.90, "method": "regex"}
			continue

		# 3) Direct synonym match
		if norm in syn_to_canon:
			result[src] = {"canonical": syn_to_canon[norm], "confidence": 0.95, "method": "synonym"}
			continue

		# 4) Fuzzy tie-breaker on synonyms (deterministic, still cheap)
		high = (None, 0.0)
		for canon, meta in truth.items():
			for s in meta.get("_syn_lc", []):
				score = normalized_similarity(norm, _normalize_header(s))
				if score > high[1]:
					high = (canon, score)
		if high[0] and high[1] >= 0.85:
			result[src] = {"canonical": high[0], "confidence": 0.82, "method": "fuzzy"}
			continue

		# 5) Unmatched (candidate for LLM)
		unmatched.append(src)

	# Optional LLM fallback
	if use_llm and unmatched:
		llm_map = map_headers_with_llm(unmatched, truth)
		for src in unmatched:
			canon = llm_map.get(src)
			if canon in truth:
				result[src] = {"canonical": canon, "confidence": 0.70, "method": "llm"}
			else:
				result[src] = {"canonical": None, "confidence": 0.0, "method": "unmapped"}
		still_unmapped = [s for s in unmatched if not result.get(s) or not result[s].get("canonical")]
		return result, still_unmapped

	return result, unmatched


def apply_mapping_overrides(
	mapping_result: Dict[str, Dict], overrides: Dict[str, str | None]
) -> Dict[str, Dict]:
	new_map: Dict[str, Dict] = {}
	for src, meta in mapping_result.items():
		canon = overrides.get(src, meta.get("canonical"))
		new_map[src] = {
			"canonical": canon,
			"confidence": meta.get("confidence", 0.0) if canon == meta.get("canonical") else 1.0,
			"method": meta.get("method", "suggested") if canon == meta.get("canonical") else "override",
		}
	return new_map
