"""
Скрипт для сбора новостей космонавтики с портала Spaceflight Now.
Парсит архивные страницы, находит ссылки на статьи и выкачивает их в сыром виде (HTML внутри JSON).
Поддерживает параллельное скачивание в несколько потоков.
"""

import argparse
import concurrent.futures
import json
import os
import re
import threading
import time
from typing import Dict, List, Optional, Set

import requests
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
            if self.n % 20 == 0 or (self.total and self.n >= self.total):
                print(f"\r[{self.desc}] {self.n}{tot}", end="", flush=True)

        def close(self):
            print()


BASE_NEWS_URL = "https://spaceflightnow.com"

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.join(CURRENT_DIR, "data_raw", "spacenews")
DEFAULT_REPORT_DIR = os.path.join(CURRENT_DIR, "out")


def safe_slug(s: str, max_len: int = 120) -> str:
    """Генерируем аккуратное имя файла для статьи."""
    s = (s or "").strip().replace(" ", "_")
    s = re.sub(r"[^0-9A-Za-z._-]+", "", s)
    s = s.strip("._-")
    return s[:max_len] if s else "doc"


def write_json(path: str, obj: Dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def get_html(session: requests.Session, url: str, timeout: int, retries: int, backoff: float) -> str:
    """Загрузка HTML страницы с повторными попытками."""
    last_err = None
    for i in range(retries + 1):
        try:
            r = session.get(url, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            time.sleep(backoff * (2 ** i))
    raise last_err


def extract_news_links(list_html: str) -> List[str]:
    """Вытаскиваем ссылки на статьи со страницы архива новостей."""
    soup = BeautifulSoup(list_html, "html.parser")
    links: List[str] = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].rstrip("/") + "/"
        # Ссылки на статьи имеют вид: https://spaceflightnow.com/ГГГГ/ММ/ДД/название/
        if re.search(r"spaceflightnow\.com/\d{4}/\d{2}/\d{2}/[^/]+/", href):
            if href not in seen:
                seen.add(href)
                links.append(href)

    return links


def parse_news_article(html: str, url: str) -> Dict[str, str]:
    """Парсим метаданные статьи: заголовок, дату, автора и абзацы текста."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()

    # Дату можно взять прямо из URL формата /YYYY/MM/DD/
    date_text = ""
    date_match = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
    if date_match:
        date_text = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
    else:
        date_meta = soup.find("meta", property="article:published_time")
        if date_meta and date_meta.get("content"):
            date_text = date_meta["content"]

    author = ""
    author_tag = soup.select_one(".author, [rel='author']")
    if author_tag:
        author = author_tag.get_text(" ", strip=True)
    else:
        author_meta = soup.find("meta", attrs={"name": "author"})
        if author_meta and author_meta.get("content"):
            author = author_meta["content"]

    # Собираем абзацы тела новости
    body_parts = []
    content_div = soup.select_one(".entry-content, article")
    search_root = content_div if content_div else soup
    for p in search_root.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if len(txt) > 35 and not any(kw in txt.lower() for kw in ["subscribe", "copyright", "terms and conditions"]):
            body_parts.append(txt)
    body = "\n\n".join(body_parts)

    return {
        "title": title,
        "date": date_text,
        "author": author,
        "body": body,
    }


def existing_ids(out_dir: str) -> Set[str]:
    """Находим уже сохраненные новости, чтобы избежать дублирования."""
    ids: Set[str] = set()
    if not os.path.isdir(out_dir):
        return ids
    for name in os.listdir(out_dir):
        if not (name.startswith("sn_") and name.endswith(".json")):
            continue
        parts = name.split("_", 2)
        if len(parts) >= 2:
            ids.add(parts[1])
    return ids


def main():
    ap = argparse.ArgumentParser(description="Сбор новостей космонавтики с портала Spaceflight Now.")
    ap.add_argument("--out_dir", default=DEFAULT_OUT_DIR, help="Куда сохранять сырые новости в JSON.")
    ap.add_argument("--report_dir", default=DEFAULT_REPORT_DIR, help="Куда сохранить отчет.")
    ap.add_argument("--max_fetch", type=int, default=4000, help="Сколько новостей скачать (цель: 4 000).")
    ap.add_argument("--max_pages", type=int, default=65, help="Сколько страниц архива обходить.")
    ap.add_argument("--sleep", type=float, default=0.08, help="Задержка между запросами.")
    ap.add_argument("--workers", type=int, default=6, help="Количество потоков скачивания.")
    ap.add_argument("--min_body_chars", type=int, default=400, help="Минимальный размер текста статьи.")
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--backoff", type=float, default=0.4)
    ap.add_argument("--ua", default="MAI-IR-Lab01-SpaceAviationCorpus/2.0 (educational; student crawler)")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(args.report_dir, exist_ok=True)

    saved_ids = existing_ids(args.out_dir)
    kept_total = len(saved_ids)
    kept_start = kept_total

    print(f"[Новости] В папке уже есть {kept_total} скачанных новостей.")
    if kept_total >= args.max_fetch:
        print(f"[Новости] Лимит в {args.max_fetch} документов уже достигнут.")
        return

    s = requests.Session()
    s.headers.update({"User-Agent": args.ua})

    fetched_ok = 0
    rejected_short = 0
    rejected_dup = 0
    errors = 0

    all_links: List[str] = []
    seen_links: Set[str] = set()

    print(f"Поиск ссылок на новости в архиве {BASE_NEWS_URL}...")
    for page in range(1, args.max_pages + 1):
        if len(seen_links) >= args.max_fetch * 2:
            break
        url = BASE_NEWS_URL if page == 1 else f"{BASE_NEWS_URL}/page/{page}/"
        try:
            html = get_html(s, url, timeout=args.timeout, retries=args.retries, backoff=args.backoff)
            page_links = extract_news_links(html)
            new_in_page = 0
            for link in page_links:
                if link not in seen_links:
                    seen_links.add(link)
                    all_links.append(link)
                    new_in_page += 1
            if new_in_page == 0 and page > 1:
                break
            time.sleep(args.sleep)
        except Exception:
            errors += 1
            break

    print(f"Найдено ссылок на новости: {len(all_links)}")

    pbar = tqdm(total=args.max_fetch, initial=kept_total, desc="Скачивание новостей")
    lock = threading.Lock()

    def process_article(link: str):
        nonlocal fetched_ok, rejected_short, rejected_dup, errors, kept_total
        slug = link.rstrip("/").split("/")[-1]
        doc_id = safe_slug(slug, max_len=60)

        with lock:
            if doc_id in saved_ids or kept_total >= args.max_fetch:
                rejected_dup += 1
                return
            saved_ids.add(doc_id)

        th_session = requests.Session()
        th_session.headers.update({"User-Agent": args.ua})

        try:
            html = get_html(th_session, link, timeout=args.timeout, retries=args.retries, backoff=args.backoff)
            with lock:
                fetched_ok += 1
        except Exception:
            with lock:
                errors += 1
            return

        meta = parse_news_article(html, link)
        if len(meta.get("body", "")) < args.min_body_chars:
            with lock:
                rejected_short += 1
            return

        doc = {
            "source": "spacenews",
            "topic": "Space & Aviation",
            "id": doc_id,
            "url": link,
            "title": meta.get("title", ""),
            "date": meta.get("date", ""),
            "author": meta.get("author", ""),
            "raw_html": html,
            "fetched_at": int(time.time()),
        }
        fname = f"sn_{doc_id}.json"
        write_json(os.path.join(args.out_dir, fname), doc)

        with lock:
            kept_total += 1
            pbar.update(1)

    candidate_links = [l for l in all_links if safe_slug(l.rstrip("/").split("/")[-1], max_len=60) not in saved_ids]

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(process_article, l) for l in candidate_links]
        for f in concurrent.futures.as_completed(futures):
            if kept_total >= args.max_fetch:
                break

    pbar.close()

    kept_new = kept_total - kept_start
    keep_rate = (kept_new / fetched_ok) if fetched_ok else 0.0

    report = {
        "source": "spacenews",
        "topic": "Space & Aviation",
        "target_max_fetch": args.max_fetch,
        "kept_total": kept_total,
        "kept_new": kept_new,
        "scanned_links": len(all_links),
        "fetched_ok": fetched_ok,
        "rejected_short": rejected_short,
        "rejected_dup": rejected_dup,
        "errors": errors,
        "keep_rate": round(keep_rate, 4),
        "ts": int(time.time()),
        "out_dir": args.out_dir,
    }
    write_json(os.path.join(args.report_dir, "spacenews_report.json"), report)

    print("\n[Новости] Сбор новостей завершен.")
    print(f"Всего сохранено новостей: {kept_total} (из них новых: {kept_new})")
    print(f"Отчет сохранен в {os.path.join(args.report_dir, 'spacenews_report.json')}")


if __name__ == "__main__":
    main()
