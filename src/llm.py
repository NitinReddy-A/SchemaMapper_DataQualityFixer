from __future__ import annotations

import os
import time
import logging
from typing import Dict, List

try:
	from openai import OpenAI
except Exception:
	OpenAI = None  # type: ignore


MODEL_HEADER = "gpt-4.1"
MODEL_CLEAN = "gpt-4.1"
LOGGER = logging.getLogger("llm")


def have_openai_key() -> bool:
	return bool(os.getenv("OPENAI_API_KEY")) and OpenAI is not None


def _client():
	if not have_openai_key():
		raise RuntimeError("OPENAI_API_KEY not configured or openai not installed")
	# Set a short timeout to avoid hanging the app
	try:
		return OpenAI(timeout=10)
	except Exception:
		return OpenAI()


def _with_retries(func, max_retries: int = 2, delay_seconds: float = 0.7):
	for attempt in range(max_retries + 1):
		try:
			return func()
		except Exception as e:
			LOGGER.warning("LLM call failed (attempt %d/%d): %s", attempt + 1, max_retries + 1, e)
			if attempt >= max_retries:
				raise
			time.sleep(delay_seconds)


def map_headers_with_llm(unmatched: List[str], truth: Dict) -> Dict[str, str]:
	"""Ask the model to map unknown headers to canonical keys. Returns {src: canonical_or_empty}."""
	if not have_openai_key() or not unmatched:
		return {}
	client = _client()
	prompt = {
		"role": "user",
		"content": (
			"You are a strict schema mapper. Given a canonical schema keys list and unknown headers, "
			"map each unknown header to EXACTLY one canonical key or return empty if not possible. "
			"Respond as a JSON object mapping unknown->canonical_or_empty. No explanations.\n\n"
			f"Canonical keys: {list(truth.keys())}\n\nUnknown headers: {unmatched}"
		),
	}
	try:
		resp = _with_retries(lambda: client.chat.completions.create(
			model=MODEL_HEADER,
			messages=[{"role": "system", "content": "Output strictly JSON."}, prompt],
			temperature=0,
		))
		text = resp.choices[0].message.content or "{}"
		import json
		obj = json.loads(text)
		return {k: str(v) for k, v in obj.items() if isinstance(v, str)}
	except Exception as e:
		LOGGER.error("map_headers_with_llm failed: %s", e)
		return {}


def propose_schema_for_headers(headers: List[str], samples: Dict[str, List[str]]) -> Dict[str, Dict]:
	"""Ask model to propose schema entries for new headers.
	Returns {src_header: {canonical, description, example, synonyms, header_regex}} strictly.
	"""
	if not have_openai_key() or not headers:
		return {}
	client = _client()
	import json as _json
	snippets = {h: samples.get(h, [])[:5] for h in headers}
	prompt = {
		"role": "user",
		"content": (
			"You are a data schema assistant. For each unknown header, propose a canonical key and metadata.\n"
			"For every header, return an object with keys: canonical, description, example, synonyms (list), header_regex.\n"
			"- canonical: concise snake_case name.\n"
			"- description: short phrase explaining the field.\n"
			"- example: realistic example, ideally from samples.\n"
			"- synonyms: 5-12 likely header variants.\n"
			"- header_regex: case-insensitive regex that matches typical header spellings (anchor with ^ and $).\n"
			"Respond STRICTLY as JSON mapping source_header -> object. No extra text.\n\n"
			f"Unknown headers: {headers}\n\nSample values: {_json.dumps(snippets)}"
		),
	}
	try:
		resp = _with_retries(lambda: client.chat.completions.create(
			model=MODEL_HEADER,
			messages=[{"role": "system", "content": "Output strictly JSON with required keys only."}, prompt],
			temperature=0,
		))
		text = resp.choices[0].message.content or "{}"
		obj = _json.loads(text)
		# Basic shape guard
		clean: Dict[str, Dict] = {}
		for src, meta in obj.items():
			if isinstance(meta, dict):
				clean[src] = {
					"header": meta.get("canonical"),
					"description": meta.get("description"),
					"example": meta.get("example"),
					"synonyms": meta.get("synonyms", []),
					"header_regex": meta.get("header_regex"),
				}
		return clean
	except Exception as e:
		LOGGER.error("propose_schema_for_headers failed: %s", e)
		return {}


def clean_value_with_llm(column: str, value: str, description: str = "") -> str | None:
	"""Ask model for a conservative cleaned value suggestion. Must be same semantic type."""
	if not have_openai_key() or value is None or value == "":
		return None
	client = _client()
	prompt = {
		"role": "user",
		"content": (
			"Given a column name and a value that failed validation, suggest a conservative cleaned value. "
			"Do not hallucinate; if unsure, return empty. Only output the cleaned value.\n\n"
			f"Column: {column}\nDescription: {description}\nValue: {value}"
		),
	}
	try:
		resp = _with_retries(lambda: client.chat.completions.create(
			model=MODEL_CLEAN,
			messages=[{"role": "system", "content": "Return only the cleaned value, or empty if unsure."}, prompt],
			temperature=0,
		))
		return (resp.choices[0].message.content or "").strip() or None
	except Exception as e:
		LOGGER.error("clean_value_with_llm failed: %s", e)
		return None
