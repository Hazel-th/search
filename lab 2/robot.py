"""
Поисковый робот (краулер) — Лабораторная работа №2.

Требования:
  1. Единственным аргументом поисковому роботу подаётся путь до yaml-конфига:
     - Секция db: параметры базы данных (SQLite).
     - Секция logic: задержка между обкачкой страниц, параметры обхода и источников.
  2. Сохранение в базе данных документов со следующими обязательными полями:
     - url (нормализованный)
     - html («сырой» html-текст документа)
     - source (название источника)
     - fetched_at (дата обкачки в формате Unix timestamp)
  3. Остановка в любой момент: при повторном запуске робот продолжает с того
     документа, на котором остановился.
  4. Переобкачка: робот периодически переобкачивает уже имеющиеся в базе документы,
     но обновляет их только в том случае, если содержимое изменилось (Conditional GET / 304, ETag, SHA-256).
"""

import hashlib
import os
import re
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
import yaml
from bs4 import BeautifulSoup

try:
    from tqdm import tqdm
except ImportError:
    class tqdm:
        def __init__(self, iterable=None, total=None, desc="", initial=0, **kwargs):
            self.iterable = iterable
            self.total = total
            self.desc = desc
            self.n = initial

        def __iter__(self):
            for item in self.iterable:
                yield item
                self.update(1)

        def update(self, n=1):
            self.n += n
            tot = f"/{self.total}" if self.total else ""
            if self.n % 10 == 0 or (self.total and self.n >= self.total):
                print(f"\r[{self.desc}] {self.n}{tot}", end="", flush=True)

        def close(self):
            print()


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

# Параметры отслеживания, которые убираем из URL для исключения дубликатов
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "_ga", "ref", "source"
}

# Расширения файлов, которые не являются HTML-страницами
IGNORED_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".map", ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".rar", ".7z",
    ".mp3", ".mp4", ".avi", ".mkv", ".mov", ".wmv", ".webm",
    ".exe", ".dmg", ".pkg", ".deb", ".rpm"
)

# Служебные разделы Википедии без статей
WIKI_IGNORED_NAMESPACES = (
    "special:", "talk:", "user:", "user_talk:", "wikipedia:", "wikipedia_talk:",
    "file:", "file_talk:", "mediawiki:", "mediawiki_talk:", "template:",
    "template_talk:", "help:", "help_talk:", "category:", "category_talk:",
    "portal:", "portal_talk:", "draft:", "draft_talk:", "timedtext:"
)


def normalize_url(url: str) -> str:
    """
    Нормализация URL:
      - приведение схемы и домена к нижнему регистру;
      - удаление якорей (#фрагмент);
      - удаление слэшей-дубликатов;
      - удаление tracking-параметров (utm, fbclid и т.д.);
      - сортировка query-параметров.
    """
    url = (url or "").strip()
    parts = urlsplit(url)

    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    port = parts.port

    if not host or scheme not in ("http", "https"):
        return ""

    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    raw_query = parse_qsl(parts.query, keep_blank_values=True)
    clean_query = [(k, v) for k, v in raw_query if k.lower() not in TRACKING_PARAMS]
    clean_query.sort(key=lambda x: (x[0], x[1]))
    query = urlencode(clean_query, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def sha256_bytes(b: bytes) -> str:
    """Контрольная сумма SHA-256 для проверки изменения документа."""
    return hashlib.sha256(b).hexdigest()


# Схема базы данных SQLite:
# Таблица docs содержит обязательные поля (url, html, source, fetched_at)
# плюс поля для условной переобкачки (last_checked_at, etag, last_modified, content_hash)
SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  url TEXT PRIMARY KEY,
  html TEXT NOT NULL,
  source TEXT NOT NULL,
  fetched_at INTEGER NOT NULL,
  last_checked_at INTEGER NOT NULL,
  etag TEXT,
  last_modified TEXT,
  content_hash TEXT NOT NULL,
  http_status INTEGER NOT NULL DEFAULT 200
);

CREATE TABLE IF NOT EXISTS frontier (
  url TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  next_fetch_at INTEGER NOT NULL,
  depth INTEGER NOT NULL,
  discovered_at INTEGER NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_frontier_fetch ON frontier(next_fetch_at, depth);
"""


class SQLiteStore:
    def __init__(self, path: str, commit_every: int = 50):
        if not os.path.isabs(path):
            self.path = os.path.join(CURRENT_DIR, path)
        else:
            self.path = path

        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._ops = 0
        self.commit_every = max(1, int(commit_every))

    def close(self):
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass

    def commit(self):
        self.conn.commit()
        self._ops = 0

    def _maybe_commit(self):
        self._ops += 1
        if self._ops >= self.commit_every:
            self.commit()

    def upsert_frontier(self, url: str, source: str, next_fetch_at: int, depth: int, max_frontier: int):
        """Добавление ссылки в очередь или перенос времени переобкачки."""
        now = int(time.time())
        row = self.conn.execute("SELECT depth, next_fetch_at FROM frontier WHERE url=?", (url,)).fetchone()

        if row is None:
            if max_frontier > 0:
                current_count = int(self.conn.execute("SELECT COUNT(*) FROM frontier").fetchone()[0])
                if current_count >= max_frontier:
                    return
            self.conn.execute(
                """
                INSERT INTO frontier(url, source, next_fetch_at, depth, discovered_at, retry_count, last_error)
                VALUES(?,?,?,?,?,0,NULL)
                """,
                (url, source, int(next_fetch_at), int(depth), now),
            )
        else:
            old_depth, old_next = row
            # Если документ уже есть в очереди, обновляем его только если нашли путь меньшей глубины
            new_depth = min(old_depth, depth)
            self.conn.execute(
                """
                UPDATE frontier
                SET depth = ?
                WHERE url = ?
                """,
                (int(new_depth), url),
            )
        self._maybe_commit()

    def pop_next_url(self) -> Optional[Tuple[str, str, int, int]]:
        """
        Извлекает следующий URL для обкачки:
        Выбирает документ с минимальным next_fetch_at (позволяет начать ровно с того места, где остановились).
        """
        row = self.conn.execute(
            """
            SELECT url, source, depth, next_fetch_at
            FROM frontier
            ORDER BY next_fetch_at ASC, depth ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        url, source, depth, next_fetch_at = row
        return url, source, int(depth), int(next_fetch_at)

    def reschedule_frontier(self, url: str, next_fetch_at: int, error_msg: Optional[str] = None):
        """Переносит документ в очереди на будущее время (для переобкачки или повтора при сбое)."""
        if error_msg is not None:
            self.conn.execute(
                """
                UPDATE frontier
                SET next_fetch_at = ?, retry_count = retry_count + 1, last_error = ?
                WHERE url = ?
                """,
                (int(next_fetch_at), str(error_msg)[:400], url),
            )
        else:
            self.conn.execute(
                """
                UPDATE frontier
                SET next_fetch_at = ?, retry_count = 0, last_error = NULL
                WHERE url = ?
                """,
                (int(next_fetch_at), url),
            )
        self._maybe_commit()

    def remove_from_frontier(self, url: str):
        """Удаляет URL из очереди (для перманентно недоступных страниц 404/403)."""
        self.conn.execute("DELETE FROM frontier WHERE url=?", (url,))
        self._maybe_commit()

    def get_doc_meta(self, url: str) -> Optional[Tuple[Optional[str], Optional[str], str]]:
        """Получает метаданные уже скачанного документа для условного запроса."""
        row = self.conn.execute(
            "SELECT etag, last_modified, content_hash FROM docs WHERE url=?",
            (url,),
        ).fetchone()
        return row if row else None

    def upsert_doc(
        self,
        url: str,
        source: str,
        html: str,
        fetched_at: int,
        last_checked_at: int,
        etag: Optional[str],
        last_modified: Optional[str],
        content_hash: str,
        http_status: int = 200,
    ):
        """Сохранение/обновление документа в таблице docs."""
        self.conn.execute(
            """
            INSERT INTO docs(url, html, source, fetched_at, last_checked_at, etag, last_modified, content_hash, http_status)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
              html=excluded.html,
              source=excluded.source,
              fetched_at=excluded.fetched_at,
              last_checked_at=excluded.last_checked_at,
              etag=excluded.etag,
              last_modified=excluded.last_modified,
              content_hash=excluded.content_hash,
              http_status=excluded.http_status
            """,
            (url, html, source, int(fetched_at), int(last_checked_at), etag, last_modified, content_hash, int(http_status)),
        )
        self._maybe_commit()

    def mark_checked_nochange(self, url: str, now_ts: int):
        """Если документ не изменился (304 или тот же хеш), обновляем только время проверки."""
        self.conn.execute("UPDATE docs SET last_checked_at=? WHERE url=?", (int(now_ts), url))
        self._maybe_commit()

    def stats(self) -> Dict[str, int]:
        total_docs = int(self.conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0])
        total_frontier = int(self.conn.execute("SELECT COUNT(*) FROM frontier").fetchone()[0])
        now = int(time.time())
        due = int(self.conn.execute("SELECT COUNT(*) FROM frontier WHERE next_fetch_at <= ?", (now,)).fetchone()[0])
        return {
            "total_docs": total_docs,
            "total_frontier": total_frontier,
            "due_to_fetch": due,
        }


@dataclass
class SourceCfg:
    name: str
    seeds: List[str]
    allowed_domains: List[str]
    allow_prefixes: List[str]


def allowed(url: str, scfg: SourceCfg) -> bool:
    """Фильтрация ссылок: проверка разрешенного домена, префикса и отсечение не-HTML файлов."""
    if not url:
        return False
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    path = (p.path or "").lower()

    if p.scheme not in ("http", "https"):
        return False

    if host not in [d.lower() for d in scfg.allowed_domains]:
        return False

    if scfg.allow_prefixes and not any(url.startswith(pref) for pref in scfg.allow_prefixes):
        return False

    if any(path.endswith(ext) for ext in IGNORED_EXTENSIONS):
        return False

    if "wikipedia.org" in host and "/wiki/" in url:
        tail = url.split("/wiki/", 1)[1].lower()
        if any(tail.startswith(ns) for ns in WIKI_IGNORED_NAMESPACES):
            return False

    return True


def extract_links(base_url: str, html: str) -> List[str]:
    """Извлечение всех ссылок со страницы для расширения очереди."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        links.append(urljoin(base_url, href))
    return links


def fetch_url(
    session: requests.Session,
    url: str,
    prev_meta: Optional[Tuple[Optional[str], Optional[str], str]],
    timeout: int = 25,
) -> Tuple[str, Optional[str], Optional[str], Optional[str], int, bool]:
    """
    Выполняет условный HTTP GET запрос (Conditional GET):
      - Передает If-None-Match (ETag) и If-Modified-Since.
      - При коде 304 Not Modified тело не передается (changed = False).
      - Проверяет SHA-256 хеш содержимого (если хеш совпал — changed = False).
      - Если документ изменился — возвращает новый HTML и changed = True.
    """
    headers = {}
    if prev_meta:
        etag, last_mod, _ = prev_meta
        if etag:
            headers["If-None-Match"] = etag
        if last_mod:
            headers["If-Modified-Since"] = last_mod

    r = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    status = r.status_code

    if status == 304:
        return "", None, None, None, 304, False

    if status in (404, 410):
        raise FileNotFoundError(f"HTTP {status} Страница не найдена: {url}")
    if status == 403:
        raise PermissionError(f"HTTP 403 Доступ запрещен: {url}")
    if status == 429:
        retry_after = r.headers.get("Retry-After", "30")
        raise ConnectionRefusedError(f"HTTP 429 Превышен лимит запросов, Retry-After: {retry_after}")

    r.raise_for_status()

    # Проверка MIME-типа (обрабатываем только веб-страницы)
    content_type = (r.headers.get("Content-Type") or "").lower()
    if content_type and not ("text/html" in content_type or "application/xhtml+xml" in content_type):
        raise ValueError(f"Пропуск не-HTML типа '{content_type}': {url}")

    html_bytes = r.content
    html_text = r.text

    etag = r.headers.get("ETag")
    last_modified = r.headers.get("Last-Modified")
    new_hash = sha256_bytes(html_bytes)

    if prev_meta:
        _, _, old_hash = prev_meta
        if old_hash == new_hash:
            return "", etag, last_modified, new_hash, status, False

    return html_text, etag, last_modified, new_hash, status, True


def run(config_path: str):
    """
    Основная логика поискового робота.
    Единственный аргумент — путь до yaml-конфига.
    """
    if not os.path.isfile(config_path):
        if os.path.isfile(os.path.join(CURRENT_DIR, config_path)):
            config_path = os.path.join(CURRENT_DIR, config_path)
        else:
            print(f"[!] Файл конфигурации не найден: {config_path}", file=sys.stderr)
            sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 1. Данные для базы данных в секции db
    db_cfg = cfg.get("db", {})
    db_path = db_cfg.get("path", "robot.db")

    # 2. Данные для робота в секции logic
    logic = cfg.get("logic", {})
    delay = float(logic.get("delay_seconds", 0.25))
    max_pages = int(logic.get("max_pages", 5000))
    recrawl_after = int(logic.get("recrawl_after_seconds", 7 * 24 * 3600))
    ua = logic.get("user_agent", "MAI-IR-Lab02-SpaceRobot/1.0 (educational; student crawler)")
    max_links_per_page = int(logic.get("max_links_per_page", 150))
    max_frontier = int(logic.get("max_frontier", 250000))
    commit_every = int(logic.get("commit_every", 50))

    store = SQLiteStore(db_path, commit_every=commit_every)

    # Поддержка корректной остановки в любой момент (Ctrl+C / SIGINT)
    interrupted = False

    def handle_signal(sig, frame):
        nonlocal interrupted
        print("\n\n[!] Получен сигнал остановки. Завершаем текущий запрос и сохраняем состояние...")
        interrupted = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Загрузка источников из конфига
    sources: List[SourceCfg] = []
    for s in logic.get("sources", []):
        sources.append(
            SourceCfg(
                name=s["name"],
                seeds=[normalize_url(u) for u in s.get("seeds", [])],
                allowed_domains=s.get("allowed_domains", []),
                allow_prefixes=s.get("allow_prefixes", []),
            )
        )

    # Добавляем seed-ссылки в очередь (если их там еще нет)
    now = int(time.time())
    for scfg in sources:
        for u in scfg.seeds:
            if allowed(u, scfg):
                store.upsert_frontier(u, scfg.name, next_fetch_at=now, depth=0, max_frontier=max_frontier)
    store.commit()

    session = requests.Session()
    session.headers.update({"User-Agent": ua})
    source_map = {s.name: s for s in sources}

    st = store.stats()
    print("=" * 65)
    print("   ПОИСКОВЫЙ РОБОТ (ЛАБОРАТОРНАЯ РАБОТА №2)")
    print(f"   Конфиг:             {config_path}")
    print(f"   База данных:        {store.path}")
    print(f"   Пауза между URL:    {delay} сек.")
    print(f"   Период переобкачки: {recrawl_after} сек. (~{recrawl_after // 86400} дн.)")
    print(f"   Документов в базе:  {st['total_docs']}")
    print(f"   Ссылок в очереди:   {st['total_frontier']} (готово к скачиванию: {st['due_to_fetch']})")
    print("=" * 65)

    processed = 0
    not_modified_count = 0
    updated_count = 0
    new_docs_count = 0
    error_count = 0

    pbar = tqdm(total=max_pages, desc="Обкачка страниц")

    try:
        while processed < max_pages and not interrupted:
            item = store.pop_next_url()
            if not item:
                print("\n[Краулер] Очередь ссылок пуста.")
                break

            url, source_name, depth, next_fetch_at = item
            scfg = source_map.get(source_name)
            if not scfg:
                store.remove_from_frontier(url)
                continue

            current_time = int(time.time())
            # Если все ссылки в очереди запланированы на будущее (период переобкачки еще не наступил)
            if next_fetch_at > current_time:
                wait_time = next_fetch_at - current_time
                if wait_time > 5:
                    print(f"\n[Краулер] Все доступные документы обкачаны. Ближайшая переобкачка через {wait_time} сек.")
                    break
                time.sleep(wait_time)

            url = normalize_url(url)
            if not allowed(url, scfg):
                store.remove_from_frontier(url)
                continue

            prev_meta = store.get_doc_meta(url)
            is_recrawl = prev_meta is not None

            try:
                html, etag, last_mod, content_hash, status, changed = fetch_url(session, url, prev_meta)
                ts = int(time.time())

                if is_recrawl and (status == 304 or not changed):
                    # Переобкачка: документ уже есть в базе и НЕ изменился
                    store.mark_checked_nochange(url, now_ts=ts)
                    store.reschedule_frontier(url, next_fetch_at=ts + recrawl_after)
                    not_modified_count += 1
                else:
                    # Документ новый ЛИБО изменился при переобкачке
                    store.upsert_doc(
                        url=url,
                        source=source_name,
                        html=html,
                        fetched_at=ts,
                        last_checked_at=ts,
                        etag=etag,
                        last_modified=last_mod,
                        content_hash=content_hash or "",
                        http_status=status,
                    )

                    if is_recrawl:
                        updated_count += 1
                    else:
                        new_docs_count += 1

                    # Извлекаем ссылки и пополняем очередь
                    new_links = extract_links(url, html)
                    added = 0
                    for link in new_links:
                        if added >= max_links_per_page:
                            break
                        n = normalize_url(link)
                        if allowed(n, scfg):
                            store.upsert_frontier(
                                n,
                                source_name,
                                next_fetch_at=ts,
                                depth=depth + 1,
                                max_frontier=max_frontier,
                            )
                            added += 1

                    # Планируем периодическую переобкачку этого документа в будущем
                    store.reschedule_frontier(url, next_fetch_at=ts + recrawl_after)

                processed += 1
                pbar.update(1)

                if delay > 0 and not interrupted:
                    time.sleep(delay)

            except (FileNotFoundError, PermissionError) as pe:
                # Перманентная ошибка 404/403/410 — убираем из очереди
                error_count += 1
                store.remove_from_frontier(url)
                if delay > 0 and not interrupted:
                    time.sleep(delay)

            except Exception as e:
                # Временная ошибка — откладываем повтор на 10 минут
                error_count += 1
                store.reschedule_frontier(url, next_fetch_at=int(time.time()) + 600, error_msg=str(e))
                if delay > 0 and not interrupted:
                    time.sleep(delay)

    finally:
        pbar.close()
        store.commit()
        final_stats = store.stats()
        store.close()

    print("\n" + "=" * 65)
    print("   РЕЗУЛЬТАТЫ РАБОТЫ РОБОТА")
    print("=" * 65)
    print(f"Всего обработано URL:           {processed}")
    print(f"  • Новых документов добавлено: {new_docs_count}")
    print(f"  • Документов обновлено:       {updated_count}")
    print(f"  • Без изменений (304 / ETag): {not_modified_count}")
    print(f"  • Ошибок скачивания:          {error_count}")
    print(f"Итого документов в базе данных: {final_stats['total_docs']}")
    print(f"Осталось ссылок в очереди:      {final_stats['total_frontier']}")
    print("=" * 65)


if __name__ == "__main__":
    # Единственным аргументом поисковому роботу подаётся путь до yaml-конфига
    if len(sys.argv) > 1 and sys.argv[1] not in ("-h", "--help"):
        cfg_path = sys.argv[1]
    else:
        cfg_path = os.path.join(CURRENT_DIR, "config.yaml")

    run(cfg_path)
