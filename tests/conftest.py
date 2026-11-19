"""Shared test setup: runs before any test module imports the app.

The app binds its database engine at import time, so point it at a fresh temporary
SQLite file here, unconditionally, so no test can ever write to a configured database.
"""
import os
import tempfile
from pathlib import Path

TEST_DB = Path(tempfile.mkdtemp()) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ.setdefault("SECRET_KEY", "test-secret")
