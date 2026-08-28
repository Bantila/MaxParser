"""Проверка чистой логики моста. Запуск: python test_bridge.py"""

import os

os.environ.setdefault("MAX_PHONE", "+70000000000")
os.environ.setdefault("TG_TOKEN", "test")

import bridge
from bridge import escape, format_from_max, format_to_max, parse_ids, parse_load


def test_parse_load():
    assert parse_load("/load") == 10
    assert parse_load("/load 30") == 30
    assert parse_load("/load@MyBot 5") == 5  # в группе команда приходит с именем бота
    assert parse_load("/load 999") == 50  # не заливаем группу целиком
    assert parse_load("/load 0") == 1
    assert parse_load("/load абв") == 10  # мусор вместо числа - берём умолчание
    assert parse_load("привет") is None
    assert parse_load("") is None
    assert parse_load("/loadall") is None


def test_routes():
    bridge.TG_TARGETS = [111]
    bridge.TG_GROUP = -1001
    bridge.TG_TOPICS = True
    bridge.TOPICS = {"text": 2, "photo": 3, "file": 4, "all": 5}
    # личка без темы, потом профильная тема, потом "все"
    assert bridge.routes("photo") == [(111, None), (-1001, 3), (-1001, 5)]
    assert bridge.routes("text") == [(111, None), (-1001, 2), (-1001, 5)]

    # незаданная тема пропускается, дубля с "все" не будет
    bridge.TOPICS = {"text": 0, "photo": 5, "file": 0, "all": 5}
    assert bridge.routes("file") == [(111, None), (-1001, 5)]
    assert bridge.routes("photo") == [(111, None), (-1001, 5)]

    # General (номер 1) уходит без message_thread_id, иначе Bot API отвечает
    # message thread not found
    bridge.TG_TOPICS = True
    bridge.TOPICS = {"text": 1, "photo": 3, "file": 4, "all": 5}
    assert bridge.routes("text") == [(111, None), (-1001, None), (-1001, 5)]

    # тем в группе нет - всё одним потоком, номера тем игнорируются
    bridge.TG_TOPICS = False
    bridge.TOPICS = {"text": 2, "photo": 3, "file": 4, "all": 5}
    assert bridge.routes("photo") == [(111, None), (-1001, None)]
    assert bridge.routes("text") == [(111, None), (-1001, None)]

    # без группы остаётся только обычная рассылка
    bridge.TG_GROUP = 0
    assert bridge.routes("text") == [(111, None)]


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
