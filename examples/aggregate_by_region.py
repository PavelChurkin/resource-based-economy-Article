#!/usr/bin/env python3
"""Предварительная агрегация больших таблиц (до ~1 ГБ) до уровня субъекта или муниципалитета.

Проблема из issue #46: таблица Росстата может весить до 1 ГБ и не помещается в контекст ИИ.
Решение: скрипт читает CSV потоково (строка за строкой, память не зависит от размера файла),
относит каждую строку к сектору базовых потребностей по коду ОКВЭД2, суммирует по коду территории
и пишет два маленьких файла:

  * `<выход>.json` — агрегаты «территория × сектор» (для БД и карты: код ОКТМО → слой хороплета);
  * `<выход>.md`   — краткая сводка на несколько килобайт — именно её и читает ИИ.

Пример (статистический регистр `urid`, колонки уточняются по structure-файлу набора):

    python3 examples/aggregate_by_region.py data.csv out/urid_2026 \\
        --region-col OKTMO --okved-col OKVED2 --region-digits 2

    --value-col  — числовая колонка для суммирования (например, выручка из `bdboo`);
    --region-digits 2 — субъект РФ (первые 2 цифры ОКТМО), 8 — муниципальное образование.

Строки с кодами ОКВЭД2 из stop-листа (оборона и безопасность) отбрасываются до агрегации
и учитываются только счётчиком — см. отчёт №4, раздел 2.
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))
from select_rosstat_tables import CATEGORIES, SECTOR_OKVED2, STOP_OKVED2  # noqa: E402

csv.field_size_limit(sys.maxsize)


def digits(code):
    return "".join(ch for ch in (code or "") if ch.isdigit())


def build_matchers(sectors, stop):
    """Коды ОКВЭД2 иерархичны по цифрам: `35.11` → `3511` начинается с `35`."""
    sector_prefixes = sorted(((digits(p), s) for s, v in sectors.items() for p in v["okved2"]),
                             key=lambda x: -len(x[0]))
    stop_prefixes = [digits(p) for p in stop]

    def sector_of(okved):
        d = digits(okved)
        if not d:
            return None, False
        if any(d.startswith(p) for p in stop_prefixes):
            return None, True
        for prefix, sector in sector_prefixes:
            if d.startswith(prefix):
                return sector, False
        return None, False

    return sector_of


def open_text(path):
    """Росстат публикует и в UTF-8, и в Windows-1251: пробуем по первым килобайтам."""
    with open(path, "rb") as f:
        head = f.read(65536)
    for enc in ("utf-8-sig", "cp1251"):
        try:
            head.decode(enc)
            return open(path, encoding=enc, newline="")
        except UnicodeDecodeError:
            continue
    return open(path, encoding="utf-8", errors="replace", newline="")


def to_number(raw):
    try:
        return float((raw or "").replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def aggregate(path, region_col, okved_col, region_digits, value_col=None, delimiter=None):
    sector_of = build_matchers(SECTOR_OKVED2, STOP_OKVED2)
    totals = defaultdict(lambda: defaultdict(lambda: {"count": 0, "sum": 0.0}))
    stats = {"rows": 0, "matched": 0, "stop_list": 0, "no_region": 0, "other_sector": 0}
    with open_text(path) as f:
        sample = f.read(8192)
        f.seek(0)
        if delimiter is None:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        reader = csv.DictReader(f, delimiter=delimiter)
        missing = [c for c in (region_col, okved_col, value_col) if c and c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"Нет колонок {missing}; есть: {reader.fieldnames}")
        for row in reader:
            stats["rows"] += 1
            sector, stopped = sector_of(row.get(okved_col))
            if stopped:
                stats["stop_list"] += 1
                continue
            if sector is None:
                stats["other_sector"] += 1
                continue
            region = digits(row.get(region_col))[:region_digits]
            if len(region) < region_digits:
                stats["no_region"] += 1
                continue
            cell = totals[region][sector]
            cell["count"] += 1
            if value_col:
                value = to_number(row.get(value_col))
                if value is not None:
                    cell["sum"] += value
            stats["matched"] += 1
    result = {r: dict(sorted(s.items())) for r, s in sorted(totals.items())}
    return result, stats


def summary_markdown(result, stats, source, value_col):
    """Сводка для ИИ: по каждому сектору — число территорий, лидеры и аутсайдеры."""
    metric = "sum" if value_col else "count"
    lines = [
        f"# Сводка агрегации: {source}",
        "",
        f"Строк прочитано: {stats['rows']}; учтено: {stats['matched']}; "
        f"отброшено по stop-листу: {stats['stop_list']}; вне секторов: {stats['other_sector']}; "
        f"без кода территории: {stats['no_region']}.",
        f"Показатель: {'сумма ' + value_col if value_col else 'число организаций'}.",
        "",
        "| Сектор | Территорий | Всего | Топ-3 территорий | Минимум |",
        "|--------|-----------:|------:|------------------|---------|",
    ]
    for sector in SECTOR_OKVED2:
        values = sorted(((cells[sector][metric], region) for region, cells in result.items() if sector in cells),
                        reverse=True)
        if not values:
            lines.append(f"| {CATEGORIES[sector]} | 0 | 0 | — | — |")
            continue
        total = sum(v for v, _ in values)
        top = ", ".join(f"{r}: {v:g}" for v, r in values[:3])
        low = f"{values[-1][1]}: {values[-1][0]:g}"
        lines.append(f"| {CATEGORIES[sector]} | {len(values)} | {total:g} | {top} | {low} |")
    lines += ["", "Коды территорий — первые цифры ОКТМО; расшифровка — справочник `oktmo` из реестра Росстата."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("input", help="CSV-файл (data-*.csv набора)")
    ap.add_argument("output", help="префикс выходных файлов (без расширения)")
    ap.add_argument("--region-col", required=True)
    ap.add_argument("--okved-col", required=True)
    ap.add_argument("--region-digits", type=int, default=2)
    ap.add_argument("--value-col")
    ap.add_argument("--delimiter")
    args = ap.parse_args(argv)

    result, stats = aggregate(args.input, args.region_col, args.okved_col, args.region_digits,
                              args.value_col, args.delimiter)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"source": Path(args.input).name, "region_digits": args.region_digits,
               "value_col": args.value_col, "stats": stats, "regions": result}
    out.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text(summary_markdown(result, stats, Path(args.input).name, args.value_col),
                                      encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
