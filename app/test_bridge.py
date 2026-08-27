"""Проверка чистой логики моста. Запуск: python test_bridge.py"""

import os

os.environ.setdefault("MAX_PHONE", "+70000000000")
os.environ.setdefault("TG_TOKEN", "test")

from bridge import escape, format_from_max, format_to_max, parse_ids


def test_parse_ids():
    assert parse_ids("123, -456  789") == [123, -456, 789]
    assert parse_ids("") == []
    assert parse_ids("12, мусор, -3") == [12, -3]  # мусор не роняет разбор
    # порядок важен: первый ID — владелец, он пишет без подписи
    assert parse_ids("5,6")[0] == 5


def test_escape():
    assert escape("<b>&") == "&lt;b&gt;&amp;"


def test_format_from_max():
    assert format_from_max("Мария", "привет") == "<b>Мария</b>\nпривет"
    assert format_from_max("Мария", None) == "<b>Мария</b>"
    # имя и текст не должны ломать HTML-разметку Telegram
    assert "<script>" not in format_from_max("<script>", "<script>")


def test_format_to_max():
    assert format_to_max("Костя", "буду", True) == "буду"
    assert format_to_max("Петя", "буду", False) == "Петя: буду"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("всё прошло")
