import os
import re
import sqlite3
import time
import logging
from contextlib import contextmanager

try:
    from assistant.config import DATABASE_PATH
except ModuleNotFoundError:
    from config import DATABASE_PATH

logger = logging.getLogger("jarvis.database")


class DatabaseManager:
    def __init__(self, db_path=DATABASE_PATH):
        self.db_path = db_path
        self._is_memory = (self.db_path == ":memory:")
        self._keepalive = None
        if self._is_memory:
            self.uri = f"file:mem_{id(self)}?mode=memory&cache=shared"
            self._keepalive = sqlite3.connect(self.uri, uri=True)
        else:
            dirname = os.path.dirname(self.db_path)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
        self.initialize_db()

    def get_connection(self):
        """
        Exposes a raw connection with WAL mode configured.
        """
        if self._is_memory:
            conn = sqlite3.connect(self.uri, uri=True)
        else:
            conn = sqlite3.connect(self.db_path)
            # Enable WAL mode for concurrent read/write performance
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def transaction(self):
        """
        Context manager for clean SQLite transaction handling.
        """
        conn = self.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Transaction failed, rolled back: {e}")
            raise e
        finally:
            conn.close()

    def initialize_db(self):
        """
        Runs the base schemas and updates schema_meta table.
        """
        schema_meta_exists = False
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta';")
            schema_meta_exists = bool(cursor.fetchone())
        except Exception as e:
            logger.error(f"Failed checking schema_meta existence: {e}")
        finally:
            conn.close()

        schemas_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas")
        if not os.path.exists(schemas_dir):
            logger.error(f"Schemas directory not found: {schemas_dir}")
            return

        schema_files = sorted([f for f in os.listdir(schemas_dir) if f.endswith(".sql")])

        applied_versions = set()
        if schema_meta_exists:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT version FROM schema_meta;")
                applied_versions = {row["version"] for row in cursor.fetchall()}
            except Exception as e:
                logger.error(f"Failed to query schema_meta: {e}")
            finally:
                conn.close()

        for filename in schema_files:
            match = re.match(r"^(\d+)", filename)
            if not match:
                continue
            version = int(match.group(1))

            if version in applied_versions:
                continue

            logger.info(f"Applying schema migration script: {filename} (Version {version})")
            filepath = os.path.join(schemas_dir, filename)

            with open(filepath, "r", encoding="utf-8") as f:
                sql_content = f.read()

            with self.transaction() as t_conn:
                t_conn.executescript(sql_content)
                t_conn.execute(
                    "INSERT INTO schema_meta (version, applied_at) VALUES (?, ?);",
                    (version, int(time.time()))
                )
            logger.info(f"Schema version {version} successfully applied.")
