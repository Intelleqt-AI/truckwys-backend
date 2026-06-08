#!/usr/bin/env python
"""
migrate_sqlite_to_postgres.py — One-time data migration from SQLite to PostgreSQL.

Run ONCE on initial deploy after:
  1. PostgreSQL is provisioned (scripts/setup_postgres.sh)
  2. DATABASE_URL is set in .env
  3. Django migrations have been applied to the new Postgres DB

Usage:
    python scripts/migrate_sqlite_to_postgres.py

The script uses Django's dumpdata/loaddata pipeline:
  1. Reads all data from SQLite (SQLITE_PATH)
  2. Dumps to a JSON fixture
  3. Loads into Postgres (DATABASE_URL)

Environment variables:
    SQLITE_PATH   Path to the SQLite file (default: db.sqlite3 in project root)
    DATABASE_URL  PostgreSQL connection URL (required in .env)
"""
import os
import sys
import subprocess
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap Django
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

SQLITE_PATH = os.environ.get("SQLITE_PATH", str(PROJECT_ROOT / "db.sqlite3"))
FIXTURE_PATH = os.environ.get("FIXTURE_PATH", str(PROJECT_ROOT / "migration_fixture.json"))


def run(cmd, env=None, check=True):
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if check and result.returncode != 0:
        sys.exit(result.returncode)
    return result


def main():
    if not Path(SQLITE_PATH).exists():
        print(f"ERROR: SQLite database not found at '{SQLITE_PATH}'")
        sys.exit(1)

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or database_url.startswith("sqlite"):
        print("ERROR: DATABASE_URL must be set to a PostgreSQL URL (not SQLite).")
        print("       Set DATABASE_URL in your .env and re-run.")
        sys.exit(1)

    manage = [sys.executable, str(PROJECT_ROOT / "manage.py")]

    # -----------------------------------------------------------------------
    # Step 1: Dump from SQLite
    # -----------------------------------------------------------------------
    print("\n[1/3] Dumping data from SQLite...")
    sqlite_env = {**os.environ, "DATABASE_URL": f"sqlite:///{SQLITE_PATH}"}

    # Exclude contenttypes and auth.Permission to avoid natural-key conflicts
    with open(FIXTURE_PATH, "w") as f:
        result = subprocess.run(
            manage + [
                "dumpdata",
                "--natural-foreign",
                "--natural-primary",
                "--exclude", "contenttypes",
                "--exclude", "auth.permission",
                "--indent", "2",
            ],
            env=sqlite_env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            sys.exit(result.returncode)
        f.write(result.stdout)

    fixture_size = Path(FIXTURE_PATH).stat().st_size
    print(f"  Fixture written to {FIXTURE_PATH} ({fixture_size:,} bytes)")

    # -----------------------------------------------------------------------
    # Step 2: Apply migrations to Postgres
    # -----------------------------------------------------------------------
    print("\n[2/3] Applying Django migrations to PostgreSQL...")
    postgres_env = {**os.environ, "DATABASE_URL": database_url}
    run(manage + ["migrate", "--run-syncdb"], env=postgres_env)

    # -----------------------------------------------------------------------
    # Step 3: Load fixture into Postgres
    # -----------------------------------------------------------------------
    print("\n[3/3] Loading fixture into PostgreSQL...")
    run(
        manage + ["loaddata", FIXTURE_PATH],
        env=postgres_env,
    )

    print("\n✓ Migration complete.")
    print(f"  You can now delete the fixture: {FIXTURE_PATH}")
    print(f"  And the old SQLite file: {SQLITE_PATH}")


if __name__ == "__main__":
    main()
