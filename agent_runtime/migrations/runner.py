from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def postgres_migrations() -> tuple[Migration, ...]:
    root = files("agent_runtime.migrations").joinpath("postgres")
    migrations = []
    for item in sorted(root.iterdir(), key=lambda entry: entry.name):
        if not item.name.endswith(".sql"):
            continue
        prefix, _, name = item.name.partition("_")
        migrations.append(Migration(int(prefix), name[:-4], item.read_text("utf-8")))
    versions = [item.version for item in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"PostgreSQL migration versions are not contiguous: {versions}")
    return tuple(migrations)


def apply_postgres_migrations(dsn: str) -> None:
    """串行应用显式 PostgreSQL 迁移。"""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "PostgreSQL storage requires: uv sync --extra postgres"
        ) from exc

    with psycopg.connect(dsn) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (0x41475254,))
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        applied = {
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        for migration in postgres_migrations():
            if migration.version in applied:
                continue
            connection.execute(migration.sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,name) VALUES(%s,%s)",
                (migration.version, migration.name),
            )
