"""README не расходится с кодом: якоря, пути, дерево проекта, таблица настроек.

README — документация, по которой заказчик запускает и сопровождает бота. Эти тесты
ловят типичное «устаревание»: переименовали раздел — ссылка из содержания ведёт
в никуда; удалили файл — README на него ссылается; добавили настройку — её нет
в таблице. Без сети, только чтение файлов проекта (и временный .env в tmp_path).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bot.config import Settings, load_settings
from tests.conftest import FAKE_TOKEN

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

# Пути, которые README упоминает, но которых в репозитории нет и быть не должно:
# их создаёт пользователь или сам бот (и они в .gitignore), либо они вне проекта.
NOT_IN_REPO = {
    ".env",  # создаётся из .env.example, секреты
    ".venv",  # создаётся uv sync
    "data/bot.db",  # база заявок, создаётся при запуске
    "data/copy.db",  # пример резервной копии
    "bot.db-wal",  # служебные файлы SQLite в режиме WAL
    "bot.db-shm",
    "../ferz-site",  # сайт лежит рядом с ботом, не в репозитории
    "main.css",  # файл сайта (../ferz-site/css/main.css)
}
# Короткие имена в тексте README даются относительно этих папок.
SEARCH_DIRS = (ROOT, ROOT / "bot", ROOT / "bot" / "assets", ROOT / "tools" / "images")
PATH_RE = re.compile(r"(^|/)[\w.-]+\.(py|md|sh|jpg|html|css|toml|lock|txt|example|db)$")


def _outside_code(text: str) -> list[str]:
    """Строки README вне блоков ```…```: «# …» в bash-примерах — не заголовки."""
    lines, in_code = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code:
            lines.append(line)
    return lines


def _code_block_after(heading: str) -> str:
    section = README.split(f"\n{heading}\n", 1)[1]
    return section.split("```", 2)[1].split("\n", 1)[1]


def _section(heading: str) -> str:
    return README.split(f"\n{heading}\n", 1)[1].split("\n## ", 1)[0]


def github_anchor(heading: str) -> str:
    """Якорь заголовка по правилам GitHub (github-slugger).

    Нижний регистр (кириллица тоже), всё кроме букв, цифр, «_», «-» и пробела
    удаляется, пробел → «-». «Настройки .env» → «настройки-env».
    """
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def test_github_anchor_rules() -> None:
    assert github_anchor("Настройки .env") == "настройки-env"
    assert (
        github_anchor("Администратор: уведомления, панель, статусы")
        == "администратор-уведомления-панель-статусы"
    )


def test_internal_links_match_headings() -> None:
    anchors: set[str] = set()
    for line in _outside_code(README):
        if m := re.match(r"#{1,6} (.+)", line):
            base = github_anchor(m.group(1))
            anchor, n = base, 0
            while anchor in anchors:  # повтор заголовка: GitHub добавляет -1, -2…
                n += 1
                anchor = f"{base}-{n}"
            anchors.add(anchor)
    links = re.findall(r"\]\(#([^)]+)\)", README)
    assert len(links) >= 14  # содержание + перекрёстные ссылки
    assert sorted(set(links) - anchors) == []


def test_relative_links_exist() -> None:
    targets = re.findall(r"\]\(([^)#:]+)\)", README)  # ](STATUS.md), без http: и #якорей
    assert targets
    assert [t for t in targets if not (ROOT / t).exists()] == []


def test_backticked_paths_exist() -> None:
    candidates = set(re.findall(r"`([^`\s]+)`", "\n".join(_outside_code(README))))
    paths = {c for c in candidates if PATH_RE.search(c) or (c.endswith("/") and "<" not in c)}
    assert len(paths) >= 30
    missing = []
    for path in sorted(paths - NOT_IN_REPO):
        exists = any(
            (base / path).exists() or (any(ch in path for ch in "*?") and any(base.glob(path)))
            for base in SEARCH_DIRS
        )
        if not exists:
            missing.append(path)
    assert missing == []


def test_project_tree_matches_files() -> None:
    """Дерево в «Структуре проекта»: всё перечисленное есть, и все модули перечислены."""
    tree = _code_block_after("## Структура проекта")
    listed: set[str] = set()
    stack: list[str] = []
    for line in tree.splitlines():
        if not line.strip():
            continue
        depth = (len(line) - len(line.lstrip(" "))) // 2
        if line.split()[0].endswith(","):  # «pyproject.toml, uv.lock, …» — файлы корня
            listed.update(name.strip() for name in line.split(","))
            continue
        name = line.split()[0]
        stack[depth:] = [name.rstrip("/")]
        listed.add("/".join(stack))
    assert [p for p in sorted(listed) if not (ROOT / p).exists()] == []

    modules = {
        p.relative_to(ROOT).as_posix()
        for folder in ("bot", "tests")
        for p in (ROOT / folder).rglob("*.py")
        if p.name != "__init__.py" or p.parent.name == "handlers"  # порядок роутеров
    }
    assert sorted(modules - listed) == []


def _settings_rows() -> list[tuple[list[str], str]]:
    """Строки таблиц раздела «Настройки .env»: (имена переменных, ячейка «По умолчанию»)."""
    rows: list[tuple[list[str], str]] = []
    default_col = None
    for line in _section("## Настройки .env").splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0] == "Переменная":  # заголовок таблицы: где столбец умолчаний
            default_col = cells.index("По умолчанию")
            continue
        assert default_col is not None
        rows.append((re.findall(r"`([A-Z_]+)`", cells[0]), cells[default_col]))
    return rows


def test_settings_table_matches_config() -> None:
    documented = [name for names, _ in _settings_rows() for name in names]
    fields = [name.upper() for name in Settings.model_fields]
    assert len(documented) == len(set(documented)), "переменная описана дважды"
    assert sorted(documented) == sorted(fields)


def test_settings_table_defaults_match_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_PATH")  # страховка conftest перекрыла бы .env
    for names, cell in _settings_rows():
        fields = [Settings.model_fields[name.lower()] for name in names]
        if cell == "—":  # обязательная
            assert all(f.is_required() for f in fields), names
            continue
        if cell.startswith("пусто"):
            assert all(f.default is None for f in fields), names
            continue
        values = re.findall(r"`([^`]+)`", cell)
        assert len(values) == len(names), names
        for name, value in zip(names, values, strict=True):
            # Значение из README, записанное в .env, даёт то же, что умолчание в коде.
            env = tmp_path / ".env"
            env.write_text(f"BOT_TOKEN={FAKE_TOKEN}\nADMIN_ID=1\n{name}={value}\n")
            loaded = getattr(load_settings(_env_file=env), name.lower())
            default = getattr(
                load_settings(_env_file=None, bot_token=FAKE_TOKEN, admin_id=1), name.lower()
            )
            assert loaded == default, name


def test_env_example_lists_every_setting() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    mentioned = set(re.findall(r"^#? ?([A-Z_]+)=", example, re.MULTILINE))
    assert mentioned == {name.upper() for name in Settings.model_fields}
