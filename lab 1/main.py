"""
Лабораторная работа №1: Сбор и анализ текстового корпуса
Тема: Космонавтика и авиация (Space & Aviation)
Целевой объем: 30 000 документов суммарно из двух источников (Wikipedia + новости)
Главный скрипт: последовательно запускает краулеры, очистку текста и подсчет статистики.
"""

import argparse
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def run_step(script_name: str, args: list = None):
    script_path = os.path.join(BASE_DIR, script_name)
    cmd = [sys.executable, script_path] + (args or [])
    print(f"\n>>> Запуск: {script_name} {' '.join(args or [])}")
    ret = subprocess.run(cmd, cwd=BASE_DIR)
    if ret.returncode != 0:
        print(f"[!] Ошибка при выполнении {script_name} (код возврата {ret.returncode})", file=sys.stderr)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Скрипт запуска Лабораторной работы №1 (тема: Космонавтика и авиация, 30 000 документов)."
    )
    parser.add_argument(
        "--step",
        choices=["all", "wiki", "spacenews", "extract", "stats"],
        default="all",
        help="Какой шаг выполнить: all (все по очереди), wiki, spacenews, extract, stats.",
    )
    parser.add_argument(
        "--target_total",
        type=int,
        default=30000,
        help="Суммарное количество документов для сбора (по умолчанию 30 000).",
    )
    parser.add_argument(
        "--max_spacenews",
        type=int,
        default=4000,
        help="Сколько новостей скачать со Spaceflight Now / SpaceNews (по умолчанию 4 000).",
    )
    parser.add_argument(
        "--max_wiki",
        type=int,
        default=None,
        help="Сколько статей скачать с Википедии (по умолчанию добирает до target_total).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Количество потоков для ускорения скачивания (по умолчанию 6).",
    )
    parser.add_argument(
        "--min_chars",
        type=int,
        default=500,
        help="Минимальная длина текста при очистке (в символах).",
    )
    args = parser.parse_args()

    news_target = args.max_spacenews
    wiki_target = args.max_wiki if args.max_wiki is not None else max(1000, args.target_total - news_target)

    print("=" * 65)
    print("   ЛАБОРАТОРНАЯ РАБОТА №1 — СБОР КОРПУСА ТЕКСТОВ")
    print("   Тема: Космонавтика и авиация (Space & Aviation)")
    print(f"   Цель: {args.target_total} документов суммарно")
    print(f"     • Новости (Spaceflight Now): {news_target}")
    print(f"     • Статьи (Wikipedia EN):     {wiki_target}")
    print("=" * 65)

    if args.step in ["all", "spacenews"]:
        ok = run_step("download_spacenews.py", ["--max_fetch", str(news_target), "--workers", str(args.workers)])
        if not ok and args.step == "all":
            print("[!] Сбор новостей прерван из-за ошибки.")
            return

    if args.step in ["all", "wiki"]:
        ok = run_step("download_wikipedia.py", ["--max_fetch", str(wiki_target), "--workers", str(args.workers)])
        if not ok and args.step == "all":
            print("[!] Сбор Википедии прерван из-за ошибки.")
            return

    if args.step in ["all", "extract"]:
        ok = run_step("extract_text.py", ["--min_chars", str(args.min_chars), "--workers", str(args.workers + 2)])
        if not ok and args.step == "all":
            print("[!] Очистка текста прервана из-за ошибки.")
            return

    if args.step in ["all", "stats"]:
        run_step("corpus_stats.py")


if __name__ == "__main__":
    main()
