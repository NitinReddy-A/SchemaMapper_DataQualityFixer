from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Tuple

import pandas as pd
from dateutil import parser as dateparser

from .schema_truth import SchemaTruth
from .llm import clean_value_with_llm, propose_schema_for_headers


@dataclass
class CleanResult:
	value: Any
	valid: bool
	reason: str | None = None
	suggestion: Any | None = None
	extra: Dict[str, Any] | None = None


NUM_STRIP_RE = re.compile(r"[ ,\t\n\r\f\v]")
CURRENCY_SYMBOLS = {"₹": "INR", "$": "USD"}
CURRENCY_SYNONYMS = {"inr": "INR", "rs": "INR", "rupees": "INR", "₹": "INR"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
ORDER_ID_RE = re.compile(r"^ORD-\d{4}$")
CUST_ID_RE = re.compile(r"^CUST-\d{1,}$")
SKU_RE = re.compile(r"^[A-Z]{2}-\d{4}$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
PIN6_RE = re.compile(r"^\d{6}$")
PHONE_CLEAN_RE = re.compile(r"[^0-9+]")


def _strip(value: Any) -> Any:
	if isinstance(value, str):
		return value.strip()
	return value


def _to_float(value: Any) -> Tuple[float | None, bool]:
	if value is None or (isinstance(value, float) and pd.isna(value)):
		return None, False
	if isinstance(value, (int, float)):
		try:
			return float(value), True
		except Exception:
			return None, False
	if isinstance(value, str):
		s = value.strip()
		s = s.replace("%", "")
		s = s.replace("₹", "")
		s = NUM_STRIP_RE.sub("", s)
		try:
			return float(s), True
		except Exception:
			return None, False
	return None, False


def _to_fraction(value: Any) -> Tuple[float | None, bool]:
	if value is None or (isinstance(value, float) and pd.isna(value)):
		return None, False
	if isinstance(value, (int, float)):
		v = float(value)
		return (v if 0 <= v <= 1 else v / 100.0), True
	if isinstance(value, str):
		s = value.strip().lower()
		if s.endswith("%"):
			s = s[:-1]
		try:
			v = float(s.replace(",", ""))
			return (v if 0 <= v <= 1 else v / 100.0), True
		except Exception:
			return None, False
	return None, False


def _to_int(value: Any) -> Tuple[int | None, bool]:
	f, ok = _to_float(value)
	if not ok or f is None:
		return None, False
	try:
		return int(round(f)), True
	except Exception:
		return None, False


def _parse_date(value: Any) -> Tuple[str | None, bool]:
	if value is None or (isinstance(value, float) and pd.isna(value)):
		return None, False
	if isinstance(value, (int, float)):
		return None, False
	try:
		dt = dateparser.parse(str(value), dayfirst=True, fuzzy=True)
		return dt.strftime("%Y-%m-%d"), True
	except Exception:
		return None, False


def _normalize_currency(value: Any) -> Tuple[str | None, bool]:
	if value is None:
		return None, False
	if isinstance(value, str):
		s = value.strip()
		if s in CURRENCY_SYMBOLS:
			return CURRENCY_SYMBOLS[s], True
		lc = s.lower()
		if lc in CURRENCY_SYNONYMS:
			return CURRENCY_SYNONYMS[lc], True
		if len(s) == 3 and s.isalpha():
			return s.upper(), True
	return None, False


def validate_and_clean(canonical: str, value: Any) -> CleanResult:
	orig = value
	v = _strip(value)

	if canonical == "order_id":
		if isinstance(v, str):
			v2 = v.upper()
			if ORDER_ID_RE.match(v2):
				return CleanResult(v2, True)
		return CleanResult(orig, False, reason="Invalid order_id format")

	if canonical == "order_date":
		parsed, ok = _parse_date(v)
		if ok and parsed:
			return CleanResult(parsed, True)
		return CleanResult(orig, False, reason="Unparseable date")

	if canonical == "customer_id":
		if isinstance(v, str):
			v2 = v.upper().replace(" ", "")
			if CUST_ID_RE.match(v2):
				return CleanResult(v2, True)
		return CleanResult(orig, False, reason="Invalid customer_id format")

	if canonical == "customer_name":
		if isinstance(v, str):
			return CleanResult(v.strip(), True)
		return CleanResult(orig, True)

	if canonical == "email":
		if isinstance(v, str):
			s = v.replace(" ", "")
			if EMAIL_RE.match(s):
				return CleanResult(s, True)
			# If value looks like a phone (7+ digits) treat as misplaced phone
			if re.search(r"\d{7,}", s):
				return CleanResult(None, False, reason="Phone found in email field")
		return CleanResult(None, False, reason="Invalid email")

	if canonical == "phone":
		if isinstance(v, str):
			s = PHONE_CLEAN_RE.sub("", v)
			digits_only = re.sub(r"[^0-9]", "", s)
			# If value contains an email, mark as misplaced email and clear phone
			if "@" in v:
				return CleanResult(None, False, reason="Email found in phone field")
			# Consider valid if at least 7 digits present
			if len(digits_only) >= 7:
				return CleanResult(s, True)
			return CleanResult(None, False, reason="Invalid phone")
		return CleanResult(orig, True)

	if canonical in {"billing_address", "shipping_address", "city", "state", "country", "product_name", "category", "subcategory"}:
		if isinstance(v, str):
			return CleanResult(v.strip(), True)
		return CleanResult(orig, True)

	if canonical == "postal_code":
		# ints/floats
		if isinstance(v, (int, float)) and not (isinstance(v, float) and pd.isna(v)):
			try:
				n = int(round(float(v)))
				if 0 <= n <= 999999:
					return CleanResult(f"{n:06d}", True)
			except Exception:
				pass
		if isinstance(v, str):
			s = v.replace(" ", "")
			if PIN6_RE.match(s):
				return CleanResult(s, True)
			# Suggest digits-only if that yields 6 digits
			ds = re.sub(r"\D", "", s)
			if len(ds) == 6:
				return CleanResult(orig, False, reason="Postal code must be 6 digits", suggestion=ds)
		return CleanResult(orig, False, reason="Postal code must be 6 digits")

	if canonical == "product_sku":
		if isinstance(v, str):
			s = v.upper().strip()
			if SKU_RE.match(s):
				return CleanResult(s, True)
		return CleanResult(orig, False, reason="Invalid SKU format")

	if canonical == "quantity":
		iv, ok = _to_int(v)
		if ok and iv is not None and iv >= 0:
			return CleanResult(iv, True)
		return CleanResult(orig, False, reason="Invalid quantity")

	if canonical == "unit_price":
		fv, ok = _to_float(v)
		if ok and fv is not None and fv >= 0:
			return CleanResult(fv, True)
		return CleanResult(orig, False, reason="Invalid unit_price")

	if canonical == "currency":
		cv, ok = _normalize_currency(v)
		if ok and cv:
			return CleanResult(cv, True)
		return CleanResult(orig, False, reason="Unknown currency")

	if canonical in {"discount_pct", "tax_pct"}:
		fv, ok = _to_fraction(v)
		if ok and fv is not None and 0 <= fv <= 1:
			return CleanResult(round(fv, 4), True)
		return CleanResult(orig, False, reason="Invalid percent value")

	if canonical == "shipping_fee":
		fv, ok = _to_float(v)
		if ok and fv is not None and fv >= 0:
			return CleanResult(fv, True)
		return CleanResult(orig, False, reason="Invalid shipping_fee")

	if canonical == "total_amount":
		fv, ok = _to_float(v)
		if ok and fv is not None and fv >= 0:
			return CleanResult(fv, True)
		return CleanResult(orig, False, reason="Invalid total_amount")

	if canonical == "tax_id":
		if isinstance(v, str):
			s = v.strip().upper()
			if GSTIN_RE.match(s):
				return CleanResult(s, True)
		return CleanResult(orig, False, reason="Invalid GSTIN format")

	return CleanResult(orig, True)


def _extract_email_from_row(raw_row: pd.Series, helper_cols: List[str]) -> str | None:
	for col in helper_cols:
		if col not in raw_row.index:
			continue
		val = str(raw_row[col]) if pd.notna(raw_row[col]) else ""
		val = val.strip()
		if not val:
			continue
		if EMAIL_RE.match(val):
			return val
		# find email within text
		m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", val)
		if m:
			return m.group(0)
	return None


def _extract_phone_from_row(raw_row: pd.Series, helper_cols: List[str]) -> str | None:
	for col in helper_cols:
		if col not in raw_row.index:
			continue
		val = str(raw_row[col]) if pd.notna(raw_row[col]) else ""
		val = val.strip()
		if not val:
			continue
		digits = re.sub(r"[^0-9+]", "", val)
		if len(re.sub(r"[^0-9]", "", digits)) >= 7:
			return digits
	return None


def build_proposed_clean_df(
	raw_df: pd.DataFrame,
	mapping_result: Dict[str, Dict],
	truth: SchemaTruth,
	clean_pack: Dict,
	use_llm: bool = False,
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
	# 0) Identify unmapped headers and propose schema (LLM) with sample values
	mapped_canon = {s: m.get("canonical") for s, m in mapping_result.items() if m.get("canonical")}
	unmapped_headers = [c for c in raw_df.columns if c not in mapped_canon]
	schema_proposals: Dict[str, Dict] = {}
	if use_llm and unmapped_headers:
		samples: Dict[str, List[str]] = {}
		for col in unmapped_headers:
			ser = raw_df[col].dropna().astype(str)
			samples[col] = ser.head(5).tolist()
		schema_proposals = propose_schema_for_headers(unmapped_headers, samples)

	# 1) Build mapped DataFrame
	source_to_canon = {s: m for s, m in mapped_canon.items() if s in raw_df.columns}
	pairs = list(source_to_canon.items())

	# Handle duplicates: take first occurrence order
	canon_seen = set()
	ordered_pairs = []
	for s, c in pairs:
		if c in canon_seen:
			continue
		canon_seen.add(c)
		ordered_pairs.append((s, c))

	mapped = {}
	for src, canon in ordered_pairs:
		mapped[canon] = raw_df[src]
	mapped_df = pd.DataFrame(mapped)

	# 2) Ensure canonical column order and include missing columns as empty
	canon_order = list(truth.keys())
	for c in canon_order:
		if c not in mapped_df.columns:
			mapped_df[c] = None
	cols_in_order = [c for c in canon_order if c in mapped_df.columns]
	proposed = mapped_df[cols_in_order].copy()

	issues: List[Dict[str, Any]] = []

	# 2.5) Prepare helper columns for extraction (email/phone hints)
	helper_cols = [c for c in raw_df.columns if any(k in c.lower() for k in ["contact", "mobile", "phone", "email", "e-mail"]) ]

	# 3) Validate and perform deterministic normalization
	for col in cols_in_order:
		series = proposed[col]
		new_values = []
		for idx, val in series.items():
			# Null/missing
			if val is None or (isinstance(val, float) and pd.isna(val)) or (isinstance(val, str) and val.strip() == ""):
				new_values.append(val)
				# Try derive suggestions for specific columns
				if col == "email":
					sugg = _extract_email_from_row(raw_df.loc[idx], helper_cols)
					issue = {"row_index": idx, "column": col, "value": val, "reason": "Null or empty"}
					if sugg:
						issue["suggestion"] = sugg
					issues.append(issue)
					continue
				if col == "phone":
					sugg = _extract_phone_from_row(raw_df.loc[idx], helper_cols)
					issue = {"row_index": idx, "column": col, "value": val, "reason": "Null or empty"}
					if sugg:
						issue["suggestion"] = sugg
					issues.append(issue)
					continue
				issues.append({"row_index": idx, "column": col, "value": val, "reason": "Null or empty"})
				continue
			res = validate_and_clean(col, val)
			new_values.append(res.value)
			if not res.valid:
				issue = {"row_index": idx, "column": col, "value": val, "reason": res.reason}
				if res.suggestion is not None:
					issue["suggestion"] = res.suggestion
				else:
					if col == "email":
						alt = _extract_email_from_row(raw_df.loc[idx], helper_cols)
						if alt:
							issue["suggestion"] = alt
					elif col == "phone":
						alt = _extract_phone_from_row(raw_df.loc[idx], helper_cols)
						if alt:
							issue["suggestion"] = alt
					elif use_llm:
						sugg = clean_value_with_llm(col, str(val), truth.get(col, {}).get("description", ""))
						if sugg is not None:
							issue["suggestion"] = sugg
				issues.append(issue)
		proposed[col] = pd.Series(new_values, index=series.index)

	# 4) Missing canonical columns summary
	missing = [c for c in canon_order if c not in proposed.columns]
	for c in missing:
		issues.append({"row_index": None, "column": c, "value": None, "reason": "Missing column (unmapped)"})

	# 5) Extra columns summary, with schema proposal if any
	extra_cols = [c for c in raw_df.columns if c not in source_to_canon]
	for c in extra_cols:
		issues.append({"row_index": None, "column": c, "value": None, "reason": "Extra column"})

	# Attach schema proposals as synthetic issues for user visibility
	for src, meta in (schema_proposals or {}).items():
		issues.append({
			"row_index": None,
			"column": src,
			"value": None,
			"reason": "New header proposal",
			"proposal": meta,
		})

	return proposed, issues
