"""
Лабораторная работа №2: Разработка поискового робота.
Скрипт запуска робота и просмотра состояния базы данных robot.db.
"""

import argparse
import os
import sqlite3
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CURRENT_DIR)

import robot


def print_db_stats(db_path: str):
    """Показывает текущее содержимое базы данных и состояние очереди."""
    if not os.path.isfile(db_path):
        print(f"[!] База данных {db_path} еще не создана. Сначала запустите робота.")
        return

    conn = sqlite3.connect(db_path)
    total_docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    total_frontier = conn.execute("SELECT COUNT(*) FROM frontier").fetchone()[0]

    sources = conn.execute("SELECT source, COUNT(*) FROM docs GROUP BY source").fetchall()

    now = int(robot.time.time())
    due = conn.execute("SELECT COUNT(*) FROM frontier WHERE next_fetch_at <= ?", (now,)).fetchone()[0]
    scheduled = conn.execute("SELECT COUNT(*) FROM frontier WHERE next_fetch_at > ?", (now,)).fetchone()[0]
    conn.close()

    print("\n" + "=" * 60)
    print("   СОСТОЯНИЕ БАЗЫ ДАННЫХ (robot.db)")
    print("=" * 60)
    print(f"Всего сохранено документов: {total_docs}")
    for src, cnt in sources:
        print(f"  • {src}: {cnt} документов")
    print(f"\nСостояние очереди ссылок (frontier):")
    print(f"  • Всего ссылок в базе:       {total_frontier}")
    print(f"  • Готово к обкачке сейчас:   {due}")
    print(f"  • Запланировано на будущее:  {scheduled}")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Запуск поискового робота (Лабораторная работа №2)."
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=os.path.join(CURRENT_DIR, "config.yaml"),
        help="Путь к YAML-конфигу (по умолчанию lab 2/config.yaml).",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Показать статистику базы данных без запуска обкачки.",
    )
    args = parser.parse_args()

    if args.stats:
        db_path = os.path.join(CURRENT_DIR, "robot.db")
        print_db_stats(db_path)
        return

    robot.run(args.config)


if __name__ == "__main__":
    main()
