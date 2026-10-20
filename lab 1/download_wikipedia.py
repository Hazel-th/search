"""
Скрипт для сбора статей из английской Википедии по теме «Космонавтика и авиация».
Использует официальный MediaWiki API: обходит дерево категорий и скачивает страницы.
Поддерживает докачку (не качает уже сохраненные статьи повторно).
"""

import argparse
import concurrent.futures
import json
import os
import re
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Set, Tuple

import requests

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
            if self.n % 25 == 0 or (self.total and self.n >= self.total):
                print(f"\r[{self.desc}] {self.n}{tot}", end="", flush=True)

        def close(self):
            print()


WIKI_API = "https://en.wikipedia.org/w/api.php"

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.join(CURRENT_DIR, "data_raw", "wikipedia_en")
DEFAULT_REPORT_DIR = os.path.join(CURRENT_DIR, "out")

# Базовые категории Википедии по нашей теме
DEFAULT_SEED_CATEGORIES = [
    "Space exploration",
    "Astronautics",
    "Human spaceflight",
    "Spacecraft",
    "Aviation",
    "Aircraft",
    "Aerospace engineering",
    "Space missions",
    "Artificial satellites",
    "Rocketry",
    "Astronomical objects",
    "Astronomy",
    "NASA",
    "Soviet space program",
    "History of aviation",
    "Airlines",
    "Airports",
    "Space technology",
    "Launch vehicles",
    "Space probes",
]


def safe_slug(s: str, max_len: int = 120) -> str:
    """Делаем нормальное имя файла из заголовка статьи."""
    s = (s or "").strip().replace(" ", "_")
    s = re.sub(r"[^0-9A-Za-z._-]+", "", s)
    s = s.strip("._-")
    return s[:max_len] if s else "doc"


def write_json(path: str, obj: Dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def api_get(session: requests.Session, params: Dict, timeout: int, retries: int, backoff: float) -> Dict:
    """Делаем запрос к API Википедии с повторами при сбоях."""
    last_err = None
    for i in range(retries + 1):
        try:
            r = session.get(WIKI_API, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(backoff * (2 ** i))
    raise last_err


def list_category_members(
    session: requests.Session,
    cat: str,
    cmtype: str,
    limit: int,
    sleep_s: float,
    timeout: int,
    retries: int,
    backoff: float,
) -> List[Dict]:
    """Получаем список страниц или подкатегорий внутри категории."""
    out: List[Dict] = []
    cont = None
    cmnamespace = 0 if cmtype == "page" else 14

    while True:
        params = {
            "action": "query",
            "format": "json",
            "list": "categorymembers",
            "cmtitle": f"Category:{cat}",
            "cmtype": cmtype,
            "cmlimit": 500,
            "cmnamespace": cmnamespace,
        }
        if cont:
            params["cmcontinue"] = cont

        data = api_get(session, params, timeout=timeout, retries=retries, backoff=backoff)
        members = data.get("query", {}).get("categorymembers", [])
        out.extend(members)

        cont = data.get("continue", {}).get("cmcontinue")
        if not cont or len(out) >= limit:
            break

        time.sleep(sleep_s)

    return out[:limit]


def fetch_page_html(
    session: requests.Session,
    pageid: int,
    sleep_s: float,
    timeout: int,
    retries: int,
    backoff: float,
) -> Optional[str]:
    """Скачиваем сырой HTML статьи по её pageid."""
    params = {
        "action": "parse",
        "format": "json",
        "pageid": pageid,
        "prop": "text",
        "disableeditsection": 1,
        "redirects": 1,
    }
    data = api_get(session, params, timeout=timeout, retries=retries, backoff=backoff)
    if sleep_s > 0:
        time.sleep(sleep_s)
    parse = data.get("parse") or {}
    return (parse.get("text") or {}).get("*")


def existing_pageids(out_dir: str) -> Set[int]:
    """Смотрим, какие статьи уже скачаны, чтобы не качать заново."""
    ids: Set[int] = set()
    if not os.path.isdir(out_dir):
        return ids
    for name in os.listdir(out_dir):
        if not (name.startswith("wiki_") and name.endswith(".json")):
            continue
        parts = name.split("_", 2)
        if len(parts) >= 2 and parts[1].isdigit():
            ids.add(int(parts[1]))
    return ids


def main():
    ap = argparse.ArgumentParser(description="Сбор статей из английской Википедии по теме Космонавтики и авиации.")
    ap.add_argument("--out_dir", default=DEFAULT_OUT_DIR, help="Куда сохранять сырые JSON (по умолчанию lab 1/data_raw/wikipedia_en).")
    ap.add_argument("--report_dir", default=DEFAULT_REPORT_DIR, help="Куда сохранить отчет (по умолчанию lab 1/out).")
    ap.add_argument("--max_fetch", type=int, default=26000, help="Сколько статей нужно скачать (цель: 26 000).")
    ap.add_argument("--min_html_chars", type=int, default=2500, help="Минимальный размер HTML статьи.")
    ap.add_argument("--max_depth", type=int, default=6, help="Глубина спуска по подкатегориям.")
    ap.add_argument("--sleep", type=float, default=0.05, help="Пауза между запросами.")
    ap.add_argument("--workers", type=int, default=6, help="Количество параллельных потоков скачивания.")
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--backoff", type=float, default=0.4)
    ap.add_argument("--cat_page_limit", type=int, default=10000)
    ap.add_argument("--cat_sub_limit", type=int, default=2000)
    ap.add_argument("--ua", default="MAI-IR-Lab01-SpaceAviationCorpus/2.0 (educational; student crawler)")
    ap.add_argument("--seed_categories", nargs="+", default=DEFAULT_SEED_CATEGORIES)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(args.report_dir, exist_ok=True)

    saved_ids = existing_pageids(args.out_dir)
    kept_total = len(saved_ids)
    kept_start = kept_total

    print(f"[Википедия] В папке уже есть {kept_total} скачанных статей.")
    if kept_total >= args.max_fetch:
        print(f"[Википедия] Лимит в {args.max_fetch} документов уже достигнут.")
        return

    fetched_ok = 0
    rejected_short = 0
    rejected_dup = 0
    errors = 0
    scanned_pages = 0

    s = requests.Session()
    s.headers.update({"User-Agent": args.ua})

    queue: deque[Tuple[str, int]] = deque((c, 0) for c in args.seed_categories)
    seen_cats: Set[str] = set()

    pbar = tqdm(total=args.max_fetch, initial=kept_total, desc="Скачивание Википедии")
    lock = threading.Lock()

    def process_page(p_item: Tuple[Dict, str]):
        nonlocal fetched_ok, rejected_short, rejected_dup, errors, kept_total
        p, found_cat = p_item
        pid = int(p.get("pageid", 0))
        title = p.get("title", "")
        if not pid or not title:
            return

        with lock:
            if pid in saved_ids or kept_total >= args.max_fetch:
                rejected_dup += 1
                return
            saved_ids.add(pid)

        th_session = requests.Session()
        th_session.headers.update({"User-Agent": args.ua})

        try:
            html = fetch_page_html(
                th_session,
                pid,
                sleep_s=args.sleep,
                timeout=args.timeout,
                retries=args.retries,
                backoff=args.backoff,
            )
            with lock:
                fetched_ok += 1
        except Exception:
            with lock:
                errors += 1
            return

        if not html or len(html) < args.min_html_chars:
            with lock:
                rejected_short += 1
            return

        doc = {
            "source": "wikipedia_en",
            "topic": "Space & Aviation",
            "category_seed": args.seed_categories,
            "category_found": found_cat,
            "pageid": pid,
            "title": title,
            "url": f"https://en.wikipedia.org/?curid={pid}",
            "raw_html": html,
            "fetched_at": int(time.time()),
        }
        fname = f"wiki_{pid}_{safe_slug(title)}.json"
        write_json(os.path.join(args.out_dir, fname), doc)

        with lock:
            kept_total += 1
            pbar.update(1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        while queue and kept_total < args.max_fetch:
            cat, depth = queue.popleft()
            if cat in seen_cats:
                continue
            seen_cats.add(cat)

            if depth < args.max_depth:
                try:
                    subcats = list_category_members(
                        s,
                        cat,
                        cmtype="subcat",
                        limit=args.cat_sub_limit,
                        sleep_s=args.sleep,
                        timeout=args.timeout,
                        retries=args.retries,
                        backoff=args.backoff,
                    )
                    for sc in subcats:
                        title = sc.get("title", "")
                        if title.startswith("Category:"):
                            title = title.replace("Category:", "")
                        if title and title not in seen_cats:
                            queue.append((title, depth + 1))
                except Exception:
                    errors += 1

            try:
                pages = list_category_members(
                    s,
                    cat,
                    cmtype="page",
                    limit=args.cat_page_limit,
                    sleep_s=args.sleep,
                    timeout=args.timeout,
                    retries=args.retries,
                    backoff=args.backoff,
                )
            except Exception:
                errors += 1
                continue

            page_items = [(p, cat) for p in pages if int(p.get("pageid", 0)) not in saved_ids]
            scanned_pages += len(pages)

            futures = [executor.submit(process_page, item) for item in page_items]
            for f in concurrent.futures.as_completed(futures):
                if kept_total >= args.max_fetch:
                    break

    pbar.close()

    kept_new = kept_total - kept_start
    keep_rate = (kept_new / fetched_ok) if fetched_ok else 0.0

    report = {
        "source": "wikipedia_en",
        "topic": "Space & Aviation",
        "target_max_fetch": args.max_fetch,
        "kept_total": kept_total,
        "kept_new": kept_new,
        "scanned_pages": scanned_pages,
        "fetched_ok": fetched_ok,
        "rejected_short": rejected_short,
        "rejected_dup": rejected_dup,
        "errors": errors,
        "keep_rate": round(keep_rate, 4),
        "ts": int(time.time()),
        "out_dir": args.out_dir,
    }
    write_json(os.path.join(args.report_dir, "wiki_report.json"), report)

    print("\n[Википедия] Сбор статей завершен.")
    print(f"Всего сохранено документов: {kept_total} (из них новых: {kept_new})")
    print(f"Отчет сохранен в {os.path.join(args.report_dir, 'wiki_report.json')}")


if __name__ == "__main__":
    main()
