import json
import os
import re
import logging
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.persistence import load_json_file, save_json_file
from src.schema_truth import load_schema_truth, canonical_keys
from src.mapper import suggest_mapping, apply_mapping_overrides
from src.clean_validate import build_proposed_clean_df
from src.llm import have_openai_key, propose_schema_for_headers
from src.csv_loader import read_csv_fallback
from src.logging_utils import setup_logging, set_log_level

# Initialize environment
APP_TITLE = "Schema Mapper & Data Quality Fixer"
BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "app.log"
DOCS_DIR = BASE_DIR / "docs"
TRUTH_PATH = DOCS_DIR / "schema_truth_source.json"
ENV_PATH = BASE_DIR / ".env"

# Logging setup (attach streamlit handler to mirror into UI)
setup_logging(LOG_FILE, level=logging.INFO, attach_streamlit=True)
logger = logging.getLogger("app")


def _load_env_once():
	if os.environ.get("_APP_ENV_LOADED") == "1":
		return
	encoding = None
	if ENV_PATH.exists():
		try:
			with ENV_PATH.open("rb") as f:
				magic = f.read(4)
			if magic.startswith(b"\xff\xfe") or magic.startswith(b"\xfe\xff"):
				encoding = "utf-16"
			elif magic.startswith(b"\xef\xbb\xbf"):
				encoding = "utf-8-sig"
		except Exception:
			encoding = None
		# default guess
		if encoding is None:
			encoding = "utf-8"
		try:
			load_dotenv(dotenv_path=ENV_PATH, encoding=encoding, override=False)
			logger.info("Loaded .env using encoding=%s", encoding)
		except Exception:
			# last resort
			load_dotenv(dotenv_path=ENV_PATH, encoding="latin-1", override=False)
			logger.info("Loaded .env using fallback encoding=latin-1")
	else:
		load_dotenv(override=False)
		logger.info("Loaded environment variables without .env file")
	os.environ["_APP_ENV_LOADED"] = "1"


_load_env_once()

st.set_page_config(page_title=APP_TITLE, layout="wide")
st.title(APP_TITLE)

# Sidebar controls
st.sidebar.header("Workflow")
step = st.sidebar.radio(
	"Steps",
	["Upload", "Mapper", "Clean/Validate", "Targeted Fixes", "Export"],
	index=0,
)

# Log level control
level_map = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
level_choice = st.sidebar.selectbox("Log level", options=list(level_map.keys()), index=1)
set_log_level(level_map[level_choice])

# Session state init
if "raw_df" not in st.session_state:
	st.session_state.raw_df = None
if "mapping_result" not in st.session_state:
	st.session_state.mapping_result = None
if "unmatched_headers" not in st.session_state:
	st.session_state.unmatched_headers = []
if "mapper_proposals" not in st.session_state:
	st.session_state.mapper_proposals = {}
if "proposed_df" not in st.session_state:
	st.session_state.proposed_df = None
if "issues" not in st.session_state:
	st.session_state.issues = []
if "final_df" not in st.session_state:
	st.session_state.final_df = None
if "overrides" not in st.session_state:
	st.session_state.overrides = {}
if "schema_changes" not in st.session_state:
	st.session_state.schema_changes = []

# Load schema truth only
try:
	truth = load_schema_truth(TRUTH_PATH)
	logger.info("Loaded schema truth: truth_keys=%d", len(canonical_keys(truth)))
except Exception as e:
	logger.exception("Failed to load schema truth")
	st.error(f"Failed to load schema truth: {e}")
	st.stop()

# Helper to render full-height tables
TABLE_HEIGHT = 420

def show_df(df: pd.DataFrame, label: str):
	st.subheader(label)
	st.dataframe(df, use_container_width=True, height=TABLE_HEIGHT, hide_index=True)

with st.expander("Recent logs (session)", expanded=False):
	logs = st.session_state.get("log_records", [])
	if logs:
		st.code("\n".join(logs[-300:]), language="text")
	else:
		st.write("No logs yet.")

# Step 1: Upload
if step == "Upload":
	uploader = st.file_uploader("Upload CSV", type=["csv"], accept_multiple_files=False)
	if uploader is not None:
		try:
			logger.info("Reading uploaded CSV: name=%s size=%d", uploader.name, len(uploader.getvalue()))
			df, enc, kwargs = read_csv_fallback(uploader)
			logger.info("Parsed CSV with encoding=%s kwargs=%s shape=%s", enc, kwargs, df.shape)
			st.session_state.raw_df = df
			st.session_state.mapping_result = None
			st.session_state.unmatched_headers = []
			st.session_state.proposed_df = None
			st.session_state.issues = []
			st.session_state.final_df = None
			st.session_state.schema_changes = []
			st.success(f"File uploaded and stored in session. Parsed with encoding '{enc}'.")
			show_df(df, "Before (Raw)")
		except Exception as e:
			logger.exception("Failed to parse CSV")
			st.error(f"Failed to parse CSV: {e}")
	elif st.session_state.raw_df is not None:
		show_df(st.session_state.raw_df, "Before (Raw)")
	else:
		st.info("Upload a CSV to begin.")

# Step 2: Mapper
elif step == "Mapper":
	if st.session_state.raw_df is None:
		st.warning("Upload a CSV first.")
		st.stop()

	# LLM always enabled as backup (will run only when no JSON match)
	use_llm = True
	logger.info("Mapper start: columns=%s use_llm=%s", list(st.session_state.raw_df.columns), use_llm)
	if not have_openai_key():
		st.info("OpenAI key not detected. Backup LLM mapping will be skipped where needed.")

	source_headers = list(st.session_state.raw_df.columns)

	# Use only synonyms from the single truth source
	merged_synonyms: Dict[str, List[str]] = {k: (v.get("synonyms", []) if isinstance(v, dict) else []) for k, v in truth.items()}

	with st.spinner("Suggesting mapping..."):
		suggested, unmatched = suggest_mapping(
			source_headers=source_headers,
			truth=truth,
			learned_synonyms=merged_synonyms,
			use_llm=use_llm,
		)
	logger.info("Mapper result: matched=%d unmatched=%d", len(suggested), len(unmatched))
	st.session_state.mapping_result = suggested
	st.session_state.unmatched_headers = unmatched

	# LLM proposals for headers that can't map to existing truth
	mapper_proposals = {}
	if use_llm and unmatched:
		try:
			samples = {h: st.session_state.raw_df[h].dropna().astype(str).head(5).tolist() for h in unmatched if h in st.session_state.raw_df.columns}
			mapper_proposals = propose_schema_for_headers(unmatched, samples)
			logger.info("Header proposals generated: %d", len(mapper_proposals))
		except Exception as e:
			logger.warning("Header proposal generation failed: %s", e)
	st.session_state.mapper_proposals = mapper_proposals

	# Overrides UI
	st.markdown("### Review and override mappings")
	canon_options = ["— Ignore —"] + canonical_keys(truth)
	override_cols = {}
	for src in source_headers:
		row = suggested.get(src, {"canonical": None, "confidence": 0.0})
		default_idx = 0
		if row.get("canonical") in canon_options:
			default_idx = canon_options.index(row["canonical"]) if row["canonical"] else 0
		cols = st.columns([4, 3, 2, 3])
		with cols[0]:
			st.write(f"Source: **{src}**")
		with cols[1]:
			selected = st.selectbox(
				f"Map '{src}' to",
				options=canon_options,
				index=default_idx,
				key=f"ovr_{src}",
			)
		with cols[2]:
			st.write(f"Suggested: {row.get('canonical') or '—'}")
		with cols[3]:
			if src in st.session_state.mapper_proposals:
				meta = st.session_state.mapper_proposals[src]
				st.write(f"Proposed new: {meta.get('header')}\n\n{meta.get('description')}")
			else:
				st.write(" ")
		override_cols[src] = None if selected == "— Ignore —" else selected

	if st.button("Apply Mapping Overrides"):
		logger.info("Applying mapping overrides for %d columns", len(override_cols))
		mapped = apply_mapping_overrides(st.session_state.mapping_result, override_cols)
		st.session_state.mapping_result = mapped
		st.success("Overrides applied.")

	# New header proposals accept (Mapper)
	if st.session_state.mapper_proposals:
		st.markdown("### New header proposals (Mapper)")
		st.dataframe(pd.DataFrame([
			{"source_header": k, **v} for k, v in st.session_state.mapper_proposals.items()
		]), use_container_width=True, height=TABLE_HEIGHT)
		if st.button("Accept all proposals to schema truth (Mapper)"):
			truth_data = load_json_file(TRUTH_PATH, default={})
			added = []
			for src, meta in st.session_state.mapper_proposals.items():
				canon = meta.get("header")
				if not canon:
					continue
				if canon not in truth_data:
					truth_data[canon] = {
						"header": canon,
						"description": meta.get("description"),
						"example": meta.get("example"),
						"synonyms": meta.get("synonyms", []),
						"header_regex": meta.get("header_regex"),
					}
					added.append({"action": "add_header", "canonical": canon, "source": src})
			if added:
				save_json_file(TRUTH_PATH, truth_data)
				st.session_state.schema_changes.extend(added)
				st.success("Proposals added to schema truth. Rerunning to refresh mapping options...")
				st.rerun()

	# Display mapping summary
	if st.session_state.mapping_result:
		map_df = pd.DataFrame([
			{"source": s, "canonical": v.get("canonical"), "confidence": v.get("confidence", 0.0), "method": v.get("method")}
			for s, v in st.session_state.mapping_result.items()
		])
		st.dataframe(map_df, use_container_width=True, height=TABLE_HEIGHT)

	# Show unmatched
	if st.session_state.unmatched_headers:
		st.warning("Unmapped headers detected:")
		st.write(st.session_state.unmatched_headers)

# Step 3: Clean/Validate
elif step == "Clean/Validate":
	if st.session_state.raw_df is None or not st.session_state.mapping_result:
		st.warning("Complete Upload and Mapper steps first.")
		st.stop()

	use_llm_clean = True
	logger.info("Clean/Validate start: rows=%d use_llm=%s", len(st.session_state.raw_df), use_llm_clean)
	if not have_openai_key():
		st.info("OpenAI key not detected. Deterministic cleaning only.")

	with st.spinner("Building proposed cleaned DataFrame and collecting issues..."):
		proposed_df, issues = build_proposed_clean_df(
			raw_df=st.session_state.raw_df,
			mapping_result=st.session_state.mapping_result,
			truth=truth,
			clean_pack={"value_transforms": truth.get("value_transforms", {})},
			use_llm=use_llm_clean,
		)
	logger.info("Clean/Validate produced: proposed_shape=%s issues=%d", proposed_df.shape, len(issues))
	st.session_state.proposed_df = proposed_df
	st.session_state.issues = issues

	# Show before/after
	tabs = st.tabs(["Before (Raw)", "Proposed (Not Applied)", "Issues Found"])
	with tabs[0]:
		show_df(st.session_state.raw_df, "Before (Raw)")
	with tabs[1]:
		show_df(st.session_state.proposed_df, "Proposed Cleaned (Preview)")
	with tabs[2]:
		if issues:
			issues_df = pd.DataFrame(issues)
			st.dataframe(issues_df, use_container_width=True, height=TABLE_HEIGHT)
		else:
			st.success("No issues detected.")

# Step 4: Targeted Fixes
elif step == "Targeted Fixes":
	if st.session_state.proposed_df is None:
		st.warning("Run Clean/Validate first.")
		st.stop()

	issues = st.session_state.issues or []
	logger.info("Targeted Fixes: issues=%d", len(issues))
	if not issues:
		st.info("No pending issues. You can export the proposed file.")
		st.session_state.final_df = st.session_state.proposed_df.copy()
	else:
		st.subheader("Review issues and apply suggestions")
		issues_df = pd.DataFrame(issues)
		st.dataframe(issues_df, use_container_width=True, height=TABLE_HEIGHT)
		count_suggestions = sum(1 for i in issues if i.get("suggestion") is not None and i.get("row_index") is not None)
		st.write(f"Suggestions available: {count_suggestions}")
		if st.button("Apply all suggested fixes"):
			final_df = st.session_state.proposed_df.copy()
			applied = 0
			for issue in issues:
				row_idx = issue.get("row_index")
				col = issue.get("column")
				sugg = issue.get("suggestion")
				if row_idx is not None and col in final_df.columns and sugg is not None:
					try:
						final_df.at[row_idx, col] = sugg
						applied += 1
					except Exception:
						pass
			st.session_state.final_df = final_df
			st.success(f"Applied {applied} fixes.")

	# New header proposals consolidated accept (from Clean/Validate stage)
	proposals = [iss for iss in issues if iss.get("reason") == "New header proposal" and iss.get("proposal")]
	if proposals:
		st.markdown("### New header proposals")
		st.dataframe(pd.DataFrame([
			{"source_header": p["column"], **p["proposal"]} for p in proposals
		]), use_container_width=True, height=TABLE_HEIGHT)
		if st.button("Accept all proposals to schema truth"):
			truth_data = load_json_file(TRUTH_PATH, default={})
			added = []
			for p in proposals:
				meta = p["proposal"]
				canon = meta.get("header")
				if not canon:
					continue
				if canon not in truth_data:
					truth_data[canon] = {
						"header": canon,
						"description": meta.get("description"),
						"example": meta.get("example"),
						"synonyms": meta.get("synonyms", []),
						"header_regex": meta.get("header_regex"),
					}
					added.append({"action": "add_header", "canonical": canon, "source": p.get("column")})
			if added:
				save_json_file(TRUTH_PATH, truth_data)
				st.session_state.schema_changes.extend(added)
				st.success("Proposals added to schema truth.")

	# Consolidated promotions (unchanged except tracking changes)
	st.markdown("### Promote improvements (one-click)")
	candidate_synonyms: Dict[str, List[str]] = {}
	mapped = st.session_state.mapping_result or {}
	for src, row in mapped.items():
		canon = row.get("canonical")
		if not canon:
			continue
		if src.strip().lower() == canon.strip().lower():
			continue
		candidate_synonyms.setdefault(canon, [])
		if src not in candidate_synonyms[canon]:
			candidate_synonyms[canon].append(src)

	candidate_transforms: Dict[str, List[Dict[str, str]]] = {}
	for issue in issues:
		sugg = issue.get("suggestion")
		col = issue.get("column")
		val = issue.get("value")
		if sugg is not None and col:
			candidate_transforms.setdefault(col, [])
			pattern = re.escape(str(val)) if val is not None else ""
			candidate_transforms[col].append({"pattern": pattern, "suggest": str(sugg)})

	cols = st.columns(2)
	with cols[0]:
		st.write(f"Header synonyms to add: {sum(len(v) for v in candidate_synonyms.values())}")
	with cols[1]:
		st.write(f"Value transforms to record: {sum(len(v) for v in candidate_transforms.values())}")

	if st.button("Promote all suggested synonyms and transforms"):
		mem = load_json_file(LEARNED_SYNONYMS_PATH, default={})
		prom = clean_pack.get("promoted_synonyms", {})
		changed = False
		added_changes = []
		for canon, syns in candidate_synonyms.items():
			mem.setdefault(canon, [])
			prom.setdefault(canon, [])
			for s in syns:
				if s.lower() not in [x.lower() for x in mem[canon]]:
					mem[canon].append(s)
					added_changes.append({"action": "promote_synonym", "canonical": canon, "synonym": s})
					changed = True
				if s.lower() not in [x.lower() for x in prom[canon]]:
					prom[canon].append(s)
					changed = True
		if changed:
			save_json_file(LEARNED_SYNONYMS_PATH, mem)
			clean_pack["promoted_synonyms"] = prom
			save_json_file(CLEAN_PACK_PATH, clean_pack)
			st.session_state.schema_changes.extend(added_changes)

		vt = clean_pack.get("value_transforms", {})
		for col, items in candidate_transforms.items():
			vt.setdefault(col, [])
			vt[col].extend(items)
			for it in items:
				st.session_state.schema_changes.append({"action": "record_transform", "column": col, **it})
		clean_pack["value_transforms"] = vt
		save_json_file(CLEAN_PACK_PATH, clean_pack)
		st.success("Promoted all candidate synonyms and value transforms.")

	# Final DF preview
	if st.session_state.final_df is not None:
		show_df(st.session_state.final_df, "Final (After applying suggested fixes)")
	else:
		show_df(st.session_state.proposed_df, "Final (No fixes applied; same as Proposed)")

# Step 5: Export
elif step == "Export":
	if st.session_state.proposed_df is None and st.session_state.final_df is None:
		st.warning("Nothing to export. Complete prior steps.")
		st.stop()
	final_df = st.session_state.final_df if st.session_state.final_df is not None else st.session_state.proposed_df
	show_df(st.session_state.raw_df, "Before (Raw)")
	show_df(final_df, "After (Final)")
	csv_bytes = final_df.to_csv(index=False).encode("utf-8")
	st.download_button(
		label="Download CSV",
		data=csv_bytes,
		file_name="cleaned_output.csv",
		mime="text/csv",
	)
	st.markdown("### Schema changes in this session")
	changes = st.session_state.get("schema_changes", [])
	if changes:
		st.dataframe(pd.DataFrame(changes), use_container_width=True, height=TABLE_HEIGHT)
	else:
		st.write("No schema changes recorded.")
