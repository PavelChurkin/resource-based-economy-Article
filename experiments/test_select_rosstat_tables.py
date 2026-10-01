#!/usr/bin/env python3
"""Тесты отбора таблиц реестра Росстата: python3 -m unittest discover -s experiments -p 'test_*.py'"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_rosstat_tables as s  # noqa: E402

BASE = "https://rosstat.gov.ru/opendata/7708234640-"
SAMPLE = [
    {"title": "Реестр вакансий", "value": BASE + "vacancies1/meta.csv"},
    {"title": "Статистический регистр хозяйствующих субъектов", "value": BASE + "urid/meta.csv"},
    {"title": "Статистический регистр хозяйствующих субъектов", "value": BASE + "urid/meta.csv"},
    {"title": "ОСНОВНЫЕ ИТОГИ: ЧИСЛО ХОЗЯЙСТВ", "value": BASE + "VSHP1/meta.csv"},
    {"title": "СТРУКТУРА ПОСЕВНЫХ ПЛОЩАДЕЙ", "value": BASE + "VSHP2/meta.csv"},
    {"title": "Исполнение бюджета", "value": BASE + "budgetexecution/meta.csv"},
    {"title": "Численность постоянного населения", "value": BASE + "population/meta.csv"},
]


class DatasetCode(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(s.dataset_code(BASE + "urid/meta.csv"), "urid")

    def test_doubled_inn(self):
        url = "https://rosstat.gov.ru/opendata/7708234640-7708234640-okvedva/meta.csv"
        self.assertEqual(s.dataset_code(url), "okvedva")


class Classify(unittest.TestCase):
    def test_rosstat_own_vacancies_excluded(self):
        category, reason, _ = s.classify("Реестр вакансий", "vacancies1")
        self.assertIsNone(category)
        self.assertIn("вакансии в самом Росстате", reason)

    def test_vshp_is_food(self):
        self.assertEqual(s.classify("ОСНОВНЫЕ ИТОГИ: ЧИСЛО ХОЗЯЙСТВ", "VSHP1")[::2], ("food", 1))
        self.assertEqual(s.classify("СТРУКТУРА ПОСЕВНЫХ ПЛОЩАДЕЙ", "VSHP2")[::2], ("food", 3))

    def test_register_and_classifiers_are_core(self):
        self.assertEqual(s.classify("Статистический регистр", "urid")[::2], ("cross_sector", 1))
        self.assertEqual(s.classify("ОКТМО", "oktmo")[::2], ("reference", 1))

    def test_archive_is_low_priority(self):
        self.assertEqual(s.classify("(Архив) Численность населения", "population2010")[2], 3)


class Select(unittest.TestCase):
    def test_dedup_and_exclusions(self):
        selected, excluded, duplicates = s.select(SAMPLE)
        self.assertEqual(duplicates, 1)
        self.assertEqual(sum(excluded.values()), 2)
        self.assertEqual([r["code"] for r in selected], ["VSHP1", "VSHP2", "population", "urid"])

    def test_report_shape(self):
        report = s.build_report(SAMPLE, "2026-01-01_00-00-00")
        self.assertEqual(report["report"], "Отчёт_анализа_2026-01-01_00-00-00_UTC.md")
        self.assertEqual(report["categories"]["water"]["tables_total"], 0)
        self.assertEqual(report["categories"]["water"]["via_okved2"]["okved2"], ["36", "37"])
        self.assertIn("gap", report["categories"]["water"])
        for key in ("title", "value", "code", "category", "subcategory", "priority", "geo_level", "period"):
            self.assertIn(key, report["tables"][0])


@unittest.skipUnless(s.DEFAULT_INPUT.exists(), "нет исходного файла реестра")
class RealRegistry(unittest.TestCase):
    def test_every_dataset_is_classified_or_excluded_explicitly(self):
        records = json.loads(s.DEFAULT_INPUT.read_text(encoding="utf-8"))
        report = s.build_report(records, "test")
        summary = report["summary"]
        self.assertEqual(summary["selected"] + summary["excluded"], report["source"]["unique_datasets"])
        self.assertNotIn("не относится к отбираемым категориям", summary["excluded_by_reason"])
        self.assertTrue(all(t["category"] in s.CATEGORIES for t in report["tables"]))

    def test_committed_json_matches_generator(self):
        committed = sorted(s.OUTPUT_DIR.glob("Отбор_таблиц_Росстата_*_UTC.json"))
        self.assertTrue(committed, "в «Цифровой двойник» нет файла отбора")
        data = json.loads(committed[-1].read_text(encoding="utf-8"))
        records = json.loads(s.DEFAULT_INPUT.read_text(encoding="utf-8"))
        fresh = s.build_report(records, data["created_utc"])
        self.assertEqual(data, json.loads(json.dumps(fresh, ensure_ascii=False)))


if __name__ == "__main__":
    unittest.main()
