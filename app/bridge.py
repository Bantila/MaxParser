"""Мост Max <-> Telegram.

Читает школьный чат в Max и пересылает всё в Telegram (вам и в группу класса).
Обратно: доверенные люди пишут боту в Telegram, текст уходит в чат Max.

    python bridge.py chats   - показать список чатов Max и их ID
    python bridge.py         - запустить мост
"""

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv
from pymax import Client, ConsolePasswordProvider, File, Message, Photo
from pymax.types.domain.attachments import (
    AudioAttachment,
    FileAttachment,
    PhotoAttachment,
    VideoAttachment,
)


def parse_ids(raw: str) -> list[int]:
    """'123, -456  789' -> [123, -456, 789]. Мусор молча пропускаем."""
    out = []
    for part in (raw or "").replace(",", " ").split():
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out


def format_from_max(sender: str, text: str | None) -> str:
    return f"<b>{escape(sender)}</b>\n{escape(text or '')}".strip()


def escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_to_max(sender: str, text: str, is_owner: bool) -> str:
    """Чужие сообщения подписываем: они уйдут в Max с вашего аккаунта."""
    return text if is_owner else f"{sender}: {text}"


def parse_load(text: str) -> int | None:
    """'/load 30' -> 30, '/load' -> 10, не команда -> None. Не больше 50."""
    parts = (text or "").split()
    if not parts or parts[0].split("@")[0] != "/load":
        return None
    count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
    return max(1, min(count, 50))


def routes(kind: str) -> list[tuple[int, int | None]]:
    """Куда слать контент вида text/photo/file: пары (чат, тема).

    Тема None - обычный чат без тем. В тему "все" уходит всё подряд,
    в том числе то, что уже ушло в свою профильную тему.
    """
    out: list[tuple[int, int | None]] = [(chat, None) for chat in TG_TARGETS]
    if not TG_GROUP:
        return out
    if not TG_TOPICS:  # тем в группе нет - всё одним потоком
        if (TG_GROUP, None) not in out:
            out.append((TG_GROUP, None))
        return out
    for topic in (TOPICS.get(kind), TOPICS.get("all")):
        if not topic:
            continue
        # У General номер 1, но Bot API его не принимает: message thread
        # not found. В General пишут вообще без message_thread_id.
        thread = None if topic == 1 else topic
        if (TG_GROUP, thread) not in out:
            out.append((TG_GROUP, thread))
    return out


load_dotenv()

MAX_PHONE = os.getenv("MAX_PHONE", "")
MAX_PASSWORD = os.getenv("MAX_PASSWORD", "")
MAX_CHAT_ID = int(os.getenv("MAX_CHAT_ID") or 0)
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_PROXY = os.getenv("TG_PROXY", "")
TG_TARGETS = parse_ids(os.getenv("TG_TARGETS", ""))
TG_TRUSTED = parse_ids(os.getenv("TG_TRUSTED", ""))
READ_ONLY = os.getenv("READ_ONLY", "true").strip().lower() != "false"

# Группа и номера тем. Ноль в номере темы означает "в эту тему не слать".
# TG_TOPICS=1 - раскладывать по темам, 0 - слать в группу одним потоком.
TG_GROUP = int(os.getenv("TG_GROUP") or 0)
TG_TOPICS = (os.getenv("TG_TOPICS") or "0").strip() == "1"
TOPICS = {
    "text": int(os.getenv("TG_TOPIC_TEXT") or 0),
    "photo": int(os.getenv("TG_TOPIC_PHOTO") or 0),
    "file": int(os.getenv("TG_TOPIC_FILE") or 0),
    "all": int(os.getenv("TG_TOPIC_ALL") or 0),
}

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

class EnvPasswordProvider:
    """Пароль 2FA из .env — в контейнере спросить его в консоли не у кого."""

    async def get_password(self, hint: str | None = None) -> str:
        return MAX_PASSWORD


client = Client(
    phone=MAX_PHONE,
    work_dir="cache",
    session_name="max.db",
    password_provider=EnvPasswordProvider() if MAX_PASSWORD else ConsolePasswordProvider(),
)


def user_name(user) -> str:
    """У User.names неудобная форма и она меняется — берём что найдём."""
    for n in getattr(user, "names", None) or []:
        for attr in ("name", "first_name", "display_name"):
            value = n.get(attr) if isinstance(n, dict) else getattr(n, attr, None)
            if value:
                return str(value)
        if isinstance(n, str) and n:
            return n
    return f"id{getattr(user, 'id', '?')}"


async def sender_name(sender_id: int | None) -> str:
    if not sender_id:
        return "Max"
    try:
        return user_name(await client.get_user(sender_id))
    except Exception:
        return f"id{sender_id}"


# --- Telegram --------------------------------------------------------------


def tg_http() -> httpx.AsyncClient:
    """Клиент для Telegram. С российских адресов api.telegram.org обычно
    недоступен — тогда в TG_PROXY нужен прокси (socks5:// или http://)."""
    return httpx.AsyncClient(proxy=TG_PROXY or None)


async def tg(http: httpx.AsyncClient, method: str, *, thread: int | None = None, **data):
    if thread:
        data["message_thread_id"] = thread
    r = await http.post(f"{TG_API}/{method}", data=data, timeout=70)
    out = r.json()
    # Telegram отвечает 200 даже на отказ, ошибка лежит внутри тела.
    if not out.get("ok"):
        raise RuntimeError(f"{method}: {out.get('description')}")
    return out


async def tg_upload(
    http: httpx.AsyncClient,
    chat_id: int,
    name: str,
    blob: bytes,
    thread: int | None = None,
):
    data: dict[str, object] = {"chat_id": chat_id}
    if thread:
        data["message_thread_id"] = thread
    r = await http.post(
        f"{TG_API}/sendDocument",
        data=data,
        files={"document": (name, blob)},
        timeout=120,
    )
    out = r.json()
    if not out.get("ok"):
        raise RuntimeError(f"sendDocument: {out.get('description')}")


async def tg_download(http: httpx.AsyncClient, file_id: str) -> bytes | None:
    info = await tg(http, "getFile", file_id=file_id)
    path = info.get("result", {}).get("file_path")
    if not path:
        return None
    url = f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}"
    return (await http.get(url, timeout=120)).content


# --- Max -> Telegram -------------------------------------------------------


async def resolve_attach(message: Message, att) -> tuple[str, str, str] | None:
    """-> (url, имя файла, вид). Для файлов и видео ссылку надо запросить отдельно.

    Вид - "photo" или "file", он решает, в какую тему уйдёт вложение.
    """
    if isinstance(att, PhotoAttachment):
        return att.base_url, f"photo_{att.photo_id}.jpg", "photo"
    if isinstance(att, FileAttachment):
        req = await client.get_file_by_id(message.chat_id, message.id, att.file_id)
        return (req.url, att.name or f"file_{att.file_id}", "file") if req else None
    if isinstance(att, VideoAttachment):
        req = await client.get_video_by_id(message.chat_id, message.id, att.video_id)
        return (req.url, f"video_{att.video_id}.mp4", "file") if req else None
    if isinstance(att, AudioAttachment) and att.url:
        return att.url, f"audio_{att.audio_id}.ogg", "file"
    return None


@client.on_message()
async def from_max(message: Message, client: Client) -> None:
    # Исключение отсюда pymax превращает в RuntimeError и рвёт соединение
    # с Max, поэтому глушим всё на границе обработчика.
    try:
        await forward_to_telegram(message, client)
    except Exception as e:
        print(f"[max->tg] сбой: {e!r}", flush=True)


async def forward_to_telegram(message: Message, client: Client) -> None:
    """Живой поток: отсеиваем чужие чаты и собственное эхо, потом доставляем."""
    if MAX_CHAT_ID and message.chat_id != MAX_CHAT_ID:
        return
    me = getattr(client.me, "id", None)
    if me and message.sender == me:
        return  # не гоняем по кругу то, что сами же отправили
    await deliver(message)


async def deliver(message: Message) -> list[str]:
    """Собственно отправка в Telegram, без фильтров живого потока.

    /load зовёт её напрямую: в истории chat_id часто пуст, а эха тут быть
    не может, так что фильтры отсеяли бы всё подряд.

    Возвращает список ошибок доставки, чтобы /load мог их показать.
    """
    errors: list[str] = []
    name = await sender_name(message.sender)
    # Telegram — через прокси, вложения из Max — напрямую: Max ждёт российский адрес.
    async with tg_http() as http, httpx.AsyncClient() as max_http:
        if message.text:
            text = format_from_max(name, message.text)
            for chat_id, thread in routes("text"):
                try:
                    await tg(
                        http,
                        "sendMessage",
                        thread=thread,
                        chat_id=chat_id,
                        text=text,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    errors.append(f"чат {chat_id}, тема {thread}: {e}")
                    print(f"[max->tg] текст не ушёл в {chat_id}: {e}", flush=True)

        for att in message.attaches or []:
            try:
                found = await resolve_attach(message, att)
                if not found:
                    continue
                url, filename, kind = found
                blob = (await max_http.get(url, timeout=120)).content
            except Exception as e:
                errors.append(f"вложение не забрать: {e}")
                print(f"[max->tg] вложение не забрать: {e}", flush=True)
                continue
            for chat_id, thread in routes(kind):
                try:
                    await tg_upload(http, chat_id, filename, blob, thread)
                except Exception as e:
                    errors.append(f"{filename} -> чат {chat_id}, тема {thread}: {e}")
                    print(f"[max->tg] не отправить {filename}: {e}", flush=True)
    return errors


# --- Telegram -> Max -------------------------------------------------------


async def handle_load(http: httpx.AsyncClient, chat_id: int, count: int) -> None:
    """Перелить последние сообщения из чата Max — проверить, что мост доставляет."""
    if not MAX_CHAT_ID:
        await tg(http, "sendMessage", chat_id=chat_id, text="MAX_CHAT_ID не задан.")
        return

    # pymax переподключается сам, но запрос в этот момент падает с
    # "Not connected to the server" или "Ping failed". Ждём восстановления.
    for _ in range(6):
        if client.is_connected:
            break
        await asyncio.sleep(5)
    else:
        await tg(
            http,
            "sendMessage",
            chat_id=chat_id,
            text="Мост переподключается к Max. Повторите через минуту.",
        )
        return

    try:
        # Сразу после старта клиент ещё не синхронизировал чаты (chats=0),
        # и история приходит пустой. Прогреваем список перед запросом.
        chats = await client.fetch_chats()
        history = await client.fetch_history(MAX_CHAT_ID, backward=count)
    except Exception as e:
        await tg(http, "sendMessage", chat_id=chat_id, text=f"История не читается: {e}")
        return

    if not history:
        known = [c.id for c in chats]
        where = "есть" if MAX_CHAT_ID in known else "НЕТ"
        await tg(
            http,
            "sendMessage",
            chat_id=chat_id,
            text=(
                f"История пуста. MAX_CHAT_ID={MAX_CHAT_ID}, "
                f"в списке из {len(chats)} чатов его {where}.\n"
                f"Доступные: {', '.join(str(i) for i in known) or 'нет'}"
            ),
        )
        return

    history = sorted(history, key=lambda m: m.time or 0)[-count:]
    targets = len(routes("text"))
    await tg(
        http,
        "sendMessage",
        chat_id=chat_id,
        text=f"Загружаю {len(history)} шт., адресатов: {targets}.",
    )

    sent = failed = empty = 0
    last_error = ""
    for old in history:
        if not (old.text or old.attaches):
            empty += 1  # служебные записи Max: вступил в чат, создана тема
            continue
        if not old.chat_id:  # в истории поле бывает пустым
            old.chat_id = MAX_CHAT_ID
        try:
            errs = await deliver(old)
            sent += 1
            if errs:
                failed += len(errs)
                last_error = errs[-1]
        except Exception as e:
            failed += 1
            last_error = str(e)
            print(f"[load] сообщение {old.id} не ушло: {e}", flush=True)

    report = f"Готово, отправлено {sent} из {len(history)}."
    if empty:
        report += f" Пустых и служебных: {empty}."
    if failed:
        report += f" Ошибок: {failed}. Последняя: {last_error}"
    if not targets:
        report += " Адресатов ноль: проверьте TG_TARGETS и TG_GROUP."
    await tg(http, "sendMessage", chat_id=chat_id, text=report)


async def handle_tg_message(http: httpx.AsyncClient, msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    uid = user.get("id")

    if uid not in TG_TRUSTED:
        await tg(http, "sendMessage", chat_id=chat_id, text="Вам сюда писать нельзя.")
        return

    # /load читает Max, а не пишет в него, поэтому READ_ONLY ему не помеха.
    count = parse_load(msg.get("text") or "")
    if count is not None:
        await handle_load(http, chat_id, count)
        return

    if READ_ONLY:
        await tg(
            http,
            "sendMessage",
            chat_id=chat_id,
            text="Мост работает только на чтение (READ_ONLY=true).",
        )
        return
    if not MAX_CHAT_ID:
        await tg(http, "sendMessage", chat_id=chat_id, text="MAX_CHAT_ID не задан.")
        return

    name = user.get("first_name") or str(uid)
    is_owner = bool(TG_TRUSTED) and uid == TG_TRUSTED[0]
    text = msg.get("text") or msg.get("caption") or ""

    # Файл качаем сами: по url его тянул бы pymax напрямую, мимо TG_PROXY.
    attachments = []
    doc = msg.get("document")
    photos = msg.get("photo")
    if doc:
        blob = await tg_download(http, doc["file_id"])
        if blob:
            attachments.append(File(raw=blob, name=doc.get("file_name") or "file"))
    elif photos:
        blob = await tg_download(http, photos[-1]["file_id"])
        if blob:
            attachments.append(Photo(raw=blob, name="photo.jpg"))

    if not text and not attachments:
        return

    try:
        await client.send_message(
            MAX_CHAT_ID,
            text=format_to_max(name, text, is_owner) if text else None,
            attachments=attachments or None,
        )
        await tg(http, "sendMessage", chat_id=chat_id, text="Отправлено в Max.")
    except Exception as e:
        await tg(http, "sendMessage", chat_id=chat_id, text=f"Не отправилось: {e}")


async def telegram_loop() -> None:
    offset = None
    async with tg_http() as http:
        while True:
            try:
                updates = await tg(http, "getUpdates", offset=offset, timeout=50)
                for upd in updates.get("result", []):
                    offset = upd["update_id"] + 1
                    if msg := upd.get("message"):
                        await handle_tg_message(http, msg)
            except Exception as e:
                print(f"[tg] {e}", flush=True)
                await asyncio.sleep(5)


# --- Запуск ----------------------------------------------------------------


@client.on_disconnect()
async def on_disconnect(*args) -> None:
    print("[max] связь потеряна, pymax переподключается", flush=True)


@client.on_start()
async def on_start(client: Client) -> None:
    mode = "ТОЛЬКО ЧТЕНИЕ" if READ_ONLY else "чтение и отправка"
    print(f"Мост запущен, режим: {mode}", flush=True)
    if not MAX_CHAT_ID:
        print("MAX_CHAT_ID пуст: в Telegram польются ВСЕ чаты Max.", flush=True)
    asyncio.create_task(telegram_loop())


async def show_chats() -> None:
    await client.connect()
    for chat in await client.fetch_chats():
        print(f"{chat.id}\t{chat.type}\t{chat.title or '(без названия)'}")
    await client.close()


def main() -> None:
    missing = [n for n in ("MAX_PHONE", "TG_TOKEN") if not os.getenv(n)]
    if missing:
        sys.exit(f"Не заданы в .env: {', '.join(missing)}")

    if len(sys.argv) > 1 and sys.argv[1] == "chats":
        asyncio.run(show_chats())
        return

    if not TG_TARGETS:
        sys.exit("TG_TARGETS пуст — некуда пересылать.")
    asyncio.run(client.start())


if __name__ == "__main__":
    main()
