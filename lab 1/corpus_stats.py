"""
Подсчет статистики текстового корпуса для Лабораторной работы №1.
Считает ровно 4 показателя по заданию:
  1. Количество документов.
  2. Размер примеров «сырых» документов.
  3. Размер текста, выделенного из «сырых» данных.
  4. Средний размер документа, средний объём текста в документе.
"""

import argparse
import json
import os
from typing import Dict, List

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_RAW = os.path.join(CURRENT_DIR, "data_raw")
DEFAULT_DATA_TEXT = os.path.join(CURRENT_DIR, "data_text")
DEFAULT_OUT_DIR = os.path.join(CURRENT_DIR, "out")


def get_files(dir_path: str, ext: str) -> List[str]:
    if not os.path.isdir(dir_path):
        return []
    return [os.path.join(dir_path, f) for f in os.listdir(dir_path) if f.endswith(ext)]


def mb(x: float) -> float:
    return x / (1024 * 1024)


def kb(x: float) -> float:
    return x / 1024


def stats_for_source(name: str, raw_dir: str, text_dir: str) -> Dict:
    raw_files = get_files(raw_dir, ".json")
    text_files = get_files(text_dir, ".txt")

    raw_sizes = [os.path.getsize(p) for p in raw_files]
    text_sizes = [os.path.getsize(p) for p in text_files]

    total_chars = 0
    for p in text_files:
        try:
            with open(p, "r", encoding="utf-8") as f:
                total_chars += len(f.read())
        except Exception:
            pass

    raw_total_bytes = sum(raw_sizes)
    text_total_bytes = sum(text_sizes)

    return {
        "source": name,
        "docs_raw": len(raw_files),
        "docs_text": len(text_files),
        "raw_total_bytes": raw_total_bytes,
        "raw_min_bytes": min(raw_sizes) if raw_sizes else 0,
        "raw_max_bytes": max(raw_sizes) if raw_sizes else 0,
        "avg_raw_bytes": (raw_total_bytes / len(raw_files)) if raw_files else 0.0,
        "text_total_bytes": text_total_bytes,
        "total_text_chars": total_chars,
        "avg_text_bytes": (text_total_bytes / len(text_files)) if text_files else 0.0,
        "avg_text_chars": (total_chars / len(text_files)) if text_files else 0.0,
    }


def main():
    ap = argparse.ArgumentParser(description="Подсчет статистики корпуса (Лабораторная работа №1).")
    ap.add_argument("--data_raw", default=DEFAULT_DATA_RAW, help="Папка с сырыми данными (lab 1/data_raw).")
    ap.add_argument("--data_text", default=DEFAULT_DATA_TEXT, help="Папка с очищенным текстом (lab 1/data_text).")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR, help="Куда сохранить отчеты (lab 1/out).")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    sources = [
        stats_for_source("wikipedia_en", os.path.join(args.data_raw, "wikipedia_en"), os.path.join(args.data_text, "wikipedia_en")),
        stats_for_source("spacenews", os.path.join(args.data_raw, "spacenews"), os.path.join(args.data_text, "spacenews")),
    ]

    total_docs_raw = sum(s["docs_raw"] for s in sources)
    total_docs_text = sum(s["docs_text"] for s in sources)
    total_raw_bytes = sum(s["raw_total_bytes"] for s in sources)
    total_text_bytes = sum(s["text_total_bytes"] for s in sources)
    total_text_chars = sum(s["total_text_chars"] for s in sources)

    all_raw_min = min((s["raw_min_bytes"] for s in sources if s["raw_min_bytes"] > 0), default=0)
    all_raw_max = max((s["raw_max_bytes"] for s in sources), default=0)

    combined_avg_raw_bytes = (total_raw_bytes / total_docs_raw) if total_docs_raw else 0.0
    combined_avg_text_bytes = (total_text_bytes / total_docs_text) if total_docs_text else 0.0
    combined_avg_text_chars = (total_text_chars / total_docs_text) if total_docs_text else 0.0

    lines = []
    lines.append("=" * 70)
    lines.append("   СТАТИСТИЧЕСКАЯ ИНФОРМАЦИЯ О КОРПУСЕ (ЛАБОРАТОРНАЯ РАБОТА №1)")
    lines.append("   Тематика: Космонавтика и авиация (Space & Aviation)")
    lines.append("=" * 70)

    lines.append("\n1. КОЛИЧЕСТВО ДОКУМЕНТОВ:")
    lines.append(f"   • Всего сырых документов:       {total_docs_raw}")
    lines.append(f"   • Всего документов с текстом:   {total_docs_text}")
    for s in sources:
        lines.append(f"     - {s['source']}: {s['docs_text']} текстов (сырых: {s['docs_raw']})")

    lines.append("\n2. РАЗМЕР ПРИМЕРОВ «СЫРЫХ» ДОКУМЕНТОВ:")
    lines.append(f"   • Общий объём сырых данных:     {mb(total_raw_bytes):.2f} MB ({total_raw_bytes:,} байт)")
    lines.append(f"   • Диапазон примеров размеров:   от {kb(all_raw_min):.2f} KB до {kb(all_raw_max):.2f} KB")
    for s in sources:
        lines.append(f"     - {s['source']}: {mb(s['raw_total_bytes']):.2f} MB (примеры от {kb(s['raw_min_bytes']):.2f} KB до {kb(s['raw_max_bytes']):.2f} KB)")

    lines.append("\n3. РАЗМЕР ТЕКСТА, ВЫДЕЛЕННОГО ИЗ «СЫРЫХ» ДАННЫХ:")
    lines.append(f"   • Общий объём чистого текста:   {mb(total_text_bytes):.2f} MB ({total_text_bytes:,} байт)")
    lines.append(f"   • Общее количество символов:    {total_text_chars:,} символов")
    for s in sources:
        lines.append(f"     - {s['source']}: {mb(s['text_total_bytes']):.2f} MB ({s['total_text_chars']:,} символов)")

    lines.append("\n4. СРЕДНИЙ РАЗМЕР ДОКУМЕНТА, СРЕДНИЙ ОБЪЁМ ТЕКСТА В ДОКУМЕНТЕ:")
    lines.append(f"   • Средний размер сырого документа:  {kb(combined_avg_raw_bytes):.2f} KB ({combined_avg_raw_bytes:.0f} байт)")
    lines.append(f"   • Средний объём текста в документе: {kb(combined_avg_text_bytes):.2f} KB ({combined_avg_text_chars:.0f} символов)")
    for s in sources:
        lines.append(f"     - {s['source']}: ср. размер = {kb(s['avg_raw_bytes']):.2f} KB, ср. объём текста = {kb(s['avg_text_bytes']):.2f} KB ({s['avg_text_chars']:.0f} символов)")

    lines.append("=" * 70)

    report_txt = "\n".join(lines)
    with open(os.path.join(args.out, "stats.txt"), "w", encoding="utf-8") as f:
        f.write(report_txt)

    payload = {
        "topic": "Space & Aviation",
        "1_document_counts": {
            "total_docs_raw": total_docs_raw,
            "total_docs_text": total_docs_text,
            "by_source": {s["source"]: {"raw": s["docs_raw"], "text": s["docs_text"]} for s in sources},
        },
        "2_raw_documents_size": {
            "total_bytes": total_raw_bytes,
            "total_mb": round(mb(total_raw_bytes), 2),
            "min_kb": round(kb(all_raw_min), 2),
            "max_kb": round(kb(all_raw_max), 2),
            "by_source": {
                s["source"]: {
                    "total_mb": round(mb(s["raw_total_bytes"]), 2),
                    "min_kb": round(kb(s["raw_min_bytes"]), 2),
                    "max_kb": round(kb(s["raw_max_bytes"]), 2),
                }
                for s in sources
            },
        },
        "3_extracted_text_size": {
            "total_bytes": total_text_bytes,
            "total_mb": round(mb(total_text_bytes), 2),
            "total_chars": total_text_chars,
            "by_source": {
                s["source"]: {
                    "total_mb": round(mb(s["text_total_bytes"]), 2),
                    "total_chars": s["total_text_chars"],
                }
                for s in sources
            },
        },
        "4_average_sizes": {
            "avg_raw_document_kb": round(kb(combined_avg_raw_bytes), 2),
            "avg_text_per_doc_kb": round(kb(combined_avg_text_bytes), 2),
            "avg_text_per_doc_chars": round(combined_avg_text_chars, 0),
            "by_source": {
                s["source"]: {
                    "avg_raw_kb": round(kb(s["avg_raw_bytes"]), 2),
                    "avg_text_kb": round(kb(s["avg_text_bytes"]), 2),
                    "avg_text_chars": round(s["avg_text_chars"], 0),
                }
                for s in sources
            },
        },
    }

    with open(os.path.join(args.out, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(report_txt)
    print(f"\n[OK] Результаты сохранены в:")
    print(f"  - {os.path.join(args.out, 'stats.txt')}   (текстовый отчёт)")
    print(f"  - {os.path.join(args.out, 'stats.json')}  (JSON)")


if __name__ == "__main__":
    main()
