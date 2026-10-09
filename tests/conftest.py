"""Isolate module-import data loading from a developer's personal planner file."""

import os
from tempfile import TemporaryDirectory

_TEST_DATA_DIR = TemporaryDirectory(prefix="weekly-planner-tests-")
os.environ["DATA_DIR"] = _TEST_DATA_DIR.name
