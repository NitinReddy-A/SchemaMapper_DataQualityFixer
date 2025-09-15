## Schema Mapper & Data Quality Fixer

An interactive Streamlit app that maps messy CSV headers to a canonical schema, validates and cleans data deterministically, uses an LLM as a conservative backup, and lets you promote improvements (new headers, synonyms, value transforms) back into a single source of truth JSON.

### Key Features

- **Upload CSV with robust parsing**: Tries multiple encodings and parsing modes automatically.
- **Header mapping**: Exact/synonym/regex-based mapping to a canonical schema, with LLM fallback when no JSON match is found.
- **Overrides UI**: Review/override each suggested mapping.
- **Schema growth**: Propose and accept brand-new canonical headers using LLM, saved into `docs/schema_truth_source.json`.
- **Deterministic cleaning & validation**: Column-specific rules normalize values and produce issue reports with suggestions.
- **Targeted fixes**: Bulk-apply suggested fixes; preview final vs raw.
- **Promote learnings**: One-click promotion of discovered synonyms and value transforms directly into `docs/schema_truth_source.json`.
- **Export**: Download a clean, schema-aligned CSV.
- **Session Logging**: Rolling logs embedded in the UI and persisted to `logs/app.log`.

### Repository Structure

```
app.py                         # Streamlit UI: 5-step workflow
requirements.txt               # Python dependencies
src/
  csv_loader.py               # Robust CSV reader with encoding fallbacks
  mapper.py                   # Header mapping logic (synonyms/regex/LLM backup)
  clean_validate.py           # Column-wise validators and proposed DataFrame builder
  schema_truth.py             # Schema truth loader and helpers
  persistence.py              # JSON load/save utilities
  llm.py                      # OpenAI calls (backup only, guarded by key)
  logging_utils.py            # File + Streamlit log handlers
docs/
  schema_truth_source.json    # Single source of truth (canonical + synonyms + optional regex + value_transforms)
  Project6InputData*.csv      # Example inputs
  Project6StdFormat.csv       # Example standardized output
logs/
  app.log                     # Rolling application log (created at runtime)
```

### Quick Start

1. Python 3.12+ recommended. Create/activate a virtual environment.
2. Install dependencies:

```
pip install -r requirements.txt
```

3. (Optional) Create a `.env` file in the project root to enable LLM features:

```
OPENAI_API_KEY=sk-...
```

4. Run the app:

```
streamlit run app.py
```

Open the provided local URL in your browser.

### 5-Step Workflow (UI)

1. Upload

   - Upload a CSV. The app tries several encodings (`utf-8`, `utf-16`, `cp1252`, `latin-1`, etc.) and parsing modes. Parsed DataFrame is shown as "Before (Raw)".

2. Mapper

   - The app suggests a mapping from source headers to canonical schema using multiple strategies:
     - Exact canonical key match
     - Regex match from `header_regex` in the schema truth
     - Case-insensitive synonym match (from the single truth JSON)
     - LLM fallback (only when no JSON match is found)
   - Review per-column suggestions and override via a selectbox, including "— Ignore —".
   - If LLM header proposals are generated for headers that do not fit the existing schema, you can accept them to extend `docs/schema_truth_source.json`.

3. Clean/Validate

   - Builds a proposed cleaned DataFrame aligned to the canonical column order.
   - For each canonical column, deterministic validators normalize values and emit issues if invalid, with suggestions where possible.
   - Missing canonical columns and extra columns are summarized as issues.
   - LLM suggestions provide conservative fixes for invalid values (used only when deterministic logic cannot suggest a fix and API key is present).
   - Tabs show: Raw, Proposed (not yet applied), and Issues Found.

4. Targeted Fixes

   - Review the issues table. Click "Apply all suggested fixes" to materialize suggestions into the Final DataFrame.
   - See consolidated schema proposals collected during cleaning and accept them to grow the schema truth.
   - Promote learnings (single source of truth):
     - Header synonyms discovered during mapping are written into `docs/schema_truth_source.json` under each canonical's `synonyms`.
     - Value transforms recorded from issues are written into `docs/schema_truth_source.json` under top-level `value_transforms`.

5. Export
   - Preview Raw vs Final.
   - Download the Final as `cleaned_output.csv`.
   - Review a session summary of schema changes (added headers, promoted synonyms, recorded transforms).

### Canonical Schema (docs/schema_truth_source.json)

The schema is a JSON object keyed by canonical column names. Each entry includes:

```
{
  "header": "<canonical>",
  "description": "<short description>",
  "example": "<example value>",
  "synonyms": ["variant1", "variant2", ...],
  "header_regex": "^(?i)...$"  # optional, case-insensitive regex for header spelling
}
```

On startup, the app normalizes synonyms and uses any `header_regex` for matching. You can add/adjust entries to bias mapping, or accept proposals from the UI to grow this file.

Example canonical keys included by default (not exhaustive): `order_id`, `order_date`, `customer_id`, `customer_name`, `email`, `phone`, `billing_address`, `shipping_address`, `city`, `state`, `postal_code`, `country`, `product_sku`, `product_name`, `category`, `subcategory`, `quantity`, `unit_price`, `currency`, `discount_pct`, `tax_pct`, `shipping_fee`, `total_amount`, `tax_id`.

### How Mapping Works (Algorithm)

For each source header:

1. Normalize header (lowercased, whitespace collapsed, symbols harmonized, non‑alnum removed for compact comparison).
2. Check exact canonical key match.
3. Check regex match against each canonical entry's `header_regex`.
4. Check normalized synonym map (schema synonyms from the truth file).
5. If still unmatched and LLM key present, query model for a canonical mapping.

Results include `canonical`, `confidence`, and `method` (canonical, regex, synonym, fuzzy, llm, unmapped). You can override any mapping in the UI.

### How Cleaning/Validation Works

After mapping, the app builds a DataFrame ordered by canonical keys, filling missing columns with `None`. It then validates and normalizes per column:

- `order_id`: `ORD-\d{4}` uppercase enforced
- `order_date`: parsed to ISO `YYYY-MM-DD`
- `customer_id`: `CUST-<digits>` uppercase / no spaces
- `email`: strict email regex; flags phone-like strings as misplaced; suggests extraction from helper columns
- `phone`: digits and `+` only; flags emails as misplaced; suggests extraction
- `postal_code`: enforces 6 digits; suggests digit-only if derivable
- `product_sku`: `AA-0000` style
- Numeric fields (`quantity`, `unit_price`, `shipping_fee`, `total_amount`): convert with tolerant parsing; nonnegative
- Fraction fields (`discount_pct`, `tax_pct`): accept `0–1` or `%` and normalize to fraction (0–1)
- `currency`: maps symbols/synonyms to ISO-like 3-letter codes (e.g., `₹` → `INR`)
- Textuals (names/addresses/city/state/country/product/category/subcategory): trimmed
- `tax_id`: GSTIN pattern enforced

For invalid or empty values, the app records an issue with `row_index`, `column`, `value`, `reason`, and optional `suggestion`. Suggestions can come from deterministic derivations (e.g., extracting email/phone from helper columns containing words like `contact`, `mobile`, `phone`, `email`), regex-derived hints, or the optional LLM.

### Promoting Learnings

Under Targeted Fixes:

- Click "Promote all suggested synonyms and transforms to schema truth" to write:
  - Header synonyms into each canonical entry's `synonyms` array in `docs/schema_truth_source.json`
  - Value transforms into the top-level `value_transforms` object in `docs/schema_truth_source.json`

### Environment & Logging

- `.env` is loaded with UTF handling heuristics; set `OPENAI_API_KEY` to enable LLM features.
- Log level selectable in sidebar (DEBUG/INFO/WARNING/ERROR).
- Logs appear inline in the app (expander) and rotate in `logs/app.log`.

### LLM Usage

The LLM is always enabled as a backup. It is called only when:

- No JSON match is found for a header mapping
- Deterministic cleaning cannot produce a compliant value

If `OPENAI_API_KEY` is absent, these backup calls are skipped and the app continues deterministically.

### Example Data

See `docs/Project6InputData*.csv` and the standardized sample `docs/Project6StdFormat.csv` to understand input variability and the intended standardized output.

### Troubleshooting

- CSV fails to load: ensure it is actually CSV, or try saving with UTF‑8 encoding. The loader tries multiple encodings and a Python engine with auto separator.
- No mappings found: check `docs/schema_truth_source.json` and consider enabling LLM fallback. You can also add synonyms/regex.
- Too many issues: inspect the Issues tab; consider enabling LLM suggestions for targeted columns.
- New headers missing after acceptance: changes are saved to `docs/schema_truth_source.json`; if the UI doesn’t update, click rerun when prompted.

### License

MIT (adjust as needed).
