"""
Скрипт для извлечения и очистки текста из «сырых» HTML файлов.
Удаляет разметку, рекламу, шапки, навигацию и скрипты.
Сохраняет чистый читаемый текст в формате .txt (UTF-8).
Поддерживает параллельную обработку в несколько потоков.
"""

import argparse
import concurrent.futures
import json
import os
import threading
from typing import Dict

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
            if self.n % 100 == 0 or (self.total and self.n >= self.total):
                tot = f"/{self.total}" if self.total else ""
                print(f"\r[{self.desc}] {self.n}{tot}", end="", flush=True)

        def close(self):
            print()


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_RAW = os.path.join(CURRENT_DIR, "data_raw")
DEFAULT_DATA_TEXT = os.path.join(CURRENT_DIR, "data_text")
DEFAULT_OUT_DIR = os.path.join(CURRENT_DIR, "out")


def read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, obj: Dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def clean_wikipedia_html(soup: BeautifulSoup) -> str:
    """Удаляем служебные блоки Википедии: ссылки на правку, сноски, навигационные таблицы."""
    for sel in [
        "script", "style", "noscript",
        ".mw-editsection", ".navbox", ".vertical-navbox",
        ".reflist", ".references", ".catlinks", ".toc",
        ".thumb", ".sidebar", ".ambox", ".hatnote", ".sistersitebox"
    ]:
        for el in soup.select(sel):
            el.decompose()

    text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("^")]
    return "\n\n".join(lines)


def clean_spacenews_html(soup: BeautifulSoup) -> str:
    """Удаляем шапку сайта, футер, боковые панели, рекламу и кнопки подписки."""
    for sel in [
        "script", "style", "noscript", "header", "footer", "nav",
        ".site-header", ".site-footer", ".sidebar", ".newspack_global_ad",
        ".sharedaddy", ".jp-relatedposts", ".author-avatar", ".entry-meta"
    ]:
        for el in soup.select(sel):
            el.decompose()

    content_div = soup.select_one(".entry-content, article")
    search_root = content_div if content_div else soup

    paragraphs = []
    for p in search_root.find_all("p"):
        t = p.get_text(" ", strip=True)
        if t and not t.startswith("Copyright") and not t.startswith("SPACENEWS"):
            paragraphs.append(t)

    if paragraphs:
        return "\n\n".join(paragraphs)

    text = search_root.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n\n".join(lines)


def html_to_text(html: str, source: str = "") -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    if "wiki" in source.lower():
        return clean_wikipedia_html(soup)
    elif "spacenews" in source.lower():
        return clean_spacenews_html(soup)
    else:
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return "\n\n".join(lines)


def process_source_fast(raw_dir: str, out_dir: str, min_chars: int, source_name: str, workers: int = 8) -> Dict:
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.isdir(raw_dir):
        return {"raw_total": 0, "text_kept": 0, "rejected_short": 0, "min_chars": min_chars}

    filenames = [fn for fn in os.listdir(raw_dir) if fn.endswith(".json")]
    total = len(filenames)
    kept = 0
    rejected_short = 0
    lock = threading.Lock()

    pbar = tqdm(total=total, desc=f"Очистка {source_name}")

    def worker(fn: str):
        nonlocal kept, rejected_short
        fp = os.path.join(raw_dir, fn)
        try:
            doc = read_json(fp)
        except Exception:
            with lock:
                pbar.update(1)
            return

        raw_html = doc.get("raw_html", "")
        txt = html_to_text(raw_html, source=source_name)

        title = doc.get("title", "").strip()
        full_text = f"{title}\n\n{txt}" if title and not txt.startswith(title) else txt

        if len(full_text) < min_chars:
            with lock:
                rejected_short += 1
                pbar.update(1)
            return

        out_fp = os.path.join(out_dir, fn.replace(".json", ".txt"))
        with open(out_fp, "w", encoding="utf-8") as f:
            f.write(full_text)

        with lock:
            kept += 1
            pbar.update(1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(worker, filenames))

    pbar.close()

    return {
        "raw_total": total,
        "text_kept": kept,
        "rejected_short": rejected_short,
        "min_chars": min_chars,
    }


def main():
    ap = argparse.ArgumentParser(description="Извлечение чистого текста из сырых HTML-документов.")
    ap.add_argument("--min_chars", type=int, default=500, help="Минимальный размер чистого текста (в символах).")
    ap.add_argument("--workers", type=int, default=8, help="Количество потоков для очистки файлов.")
    ap.add_argument("--data_raw", default=DEFAULT_DATA_RAW, help="Папка с сырыми данными (по умолчанию lab 1/data_raw).")
    ap.add_argument("--data_text", default=DEFAULT_DATA_TEXT, help="Куда сохранять чистые .txt (по умолчанию lab 1/data_text).")
    ap.add_argument("--out_dir", default=DEFAULT_OUT_DIR, help="Куда сохранить отчет (по умолчанию lab 1/out).")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    sources = [
        ("wikipedia_en", os.path.join(args.data_raw, "wikipedia_en"), os.path.join(args.data_text, "wikipedia_en")),
        ("spacenews", os.path.join(args.data_raw, "spacenews"), os.path.join(args.data_text, "spacenews")),
    ]

    report = {}
    print(f"Очистка текстов от HTML (порог длины = {args.min_chars} симв., потоков = {args.workers})...")
    for name, raw_path, text_path in sources:
        if os.path.isdir(raw_path):
            res = process_source_fast(raw_path, text_path, args.min_chars, name, workers=args.workers)
            report[name] = res
            print(f"  {name}: сохранено {res['text_kept']} из {res['raw_total']} (пропущено коротких: {res['rejected_short']})")
        else:
            print(f"  {name}: папка {raw_path} пока пуста или не найдена (пропуск)")

    write_json(os.path.join(args.out_dir, "extract_report.json"), report)
    print(f"[OK] Тексты успешно очищены. Отчет: {os.path.join(args.out_dir, 'extract_report.json')}")


if __name__ == "__main__":
    main()
