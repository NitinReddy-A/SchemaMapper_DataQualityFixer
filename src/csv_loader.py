from __future__ import annotations

from io import BytesIO
from typing import Any, Dict, Tuple

import pandas as pd


CANDIDATE_ENCODINGS = [
	"utf-8",
	"utf-8-sig",
	"utf-16",
	"utf-16le",
	"utf-16be",
	"cp1252",
	"latin-1",
]


def read_csv_fallback(uploaded_file) -> Tuple[pd.DataFrame, str, Dict[str, Any]]:
	"""Try multiple encodings and parsing modes to read a CSV from a Streamlit UploadedFile.
	Returns (df, encoding, kwargs_used). Raises last exception if all attempts fail.
	"""
	data = uploaded_file.getvalue()
	last_exc: Exception | None = None
	for enc in CANDIDATE_ENCODINGS:
		for kwargs in ({}, {"sep": None, "engine": "python"}):
			try:
				bio = BytesIO(data)
				df = pd.read_csv(bio, encoding=enc, **kwargs)
				return df, enc, kwargs
			except Exception as e:
				last_exc = e
				continue
	if last_exc:
		raise last_exc
	raise RuntimeError("Failed to parse CSV with fallback encodings.")
