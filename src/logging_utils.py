from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


class StreamlitLogHandler(logging.Handler):
	def __init__(self):
		super().__init__()

	def emit(self, record: logging.LogRecord) -> None:
		try:
			from streamlit import session_state as st_state  # lazy import
		except Exception:
			return
		msg = self.format(record)
		logs = st_state.get("log_records", [])
		logs.append(msg)
		# keep a rolling window
		if len(logs) > 2000:
			logs = logs[-2000:]
		st_state["log_records"] = logs


def setup_logging(log_file: Path, level: int = logging.INFO, attach_streamlit: bool = False) -> None:
	log_file.parent.mkdir(parents=True, exist_ok=True)
	logger = logging.getLogger()
	logger.setLevel(level)

	# Avoid duplicate handlers on reruns
	if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
		file_handler = RotatingFileHandler(str(log_file), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
		file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
		logger.addHandler(file_handler)

	if attach_streamlit and not any(isinstance(h, StreamlitLogHandler) for h in logger.handlers):
		st_handler = StreamlitLogHandler()
		st_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
		logger.addHandler(st_handler)


def set_log_level(level: int) -> None:
	logger = logging.getLogger()
	logger.setLevel(level)
	for h in logger.handlers:
		h.setLevel(level)
