"""Файлы миграций: порядок применения задаёт имя файла."""
import pathlib
import re

MIGRATIONS = sorted((pathlib.Path(__file__).parent.parent / "migrations").glob("*.sql"))


def test_migrations_are_numbered_without_gaps():
    numbers = []
    for path in MIGRATIONS:
        match = re.match(r"^(\d{3})_[a-z0-9_]+\.sql$", path.name)
        assert match, f"имя файла миграции не по шаблону NNN_name.sql: {path.name}"
        numbers.append(int(match.group(1)))
    assert numbers == list(range(1, len(numbers) + 1))
