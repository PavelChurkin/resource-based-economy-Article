#!/usr/bin/env python3
"""Тесты потоковой агрегации: python3 -m unittest discover -s experiments -p 'test_*.py'"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))
import aggregate_by_region as a  # noqa: E402

CSV = """OKTMO;OKVED2;REVENUE
45000000;35.11;100
45000000;36.00;50,5
45000000;85.14;10
46000000;35.12;7
46000000;25.40;999
46000000;84.22;999
46000000;62.01;1
;86.10;3
"""


class Aggregate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text, encoding="utf-8"):
        path = self.dir / "data.csv"
        path.write_text(text, encoding=encoding)
        return path

    def test_sector_matching_and_stop_list(self):
        sector_of = a.build_matchers(a.SECTOR_OKVED2, a.STOP_OKVED2)
        self.assertEqual(sector_of("35.11"), ("energy", False))
        self.assertEqual(sector_of("49.10.1"), ("transport", False))
        self.assertEqual(sector_of("25.40"), (None, True))
        self.assertEqual(sector_of("84.22"), (None, True))
        self.assertEqual(sector_of("84.11"), (None, False))
        self.assertEqual(sector_of(""), (None, False))

    def test_aggregate_by_subject(self):
        result, stats = a.aggregate(self.write(CSV), "OKTMO", "OKVED2", 2, "REVENUE")
        self.assertEqual(stats, {"rows": 8, "matched": 4, "stop_list": 2, "no_region": 1, "other_sector": 1})
        self.assertEqual(result["45"]["energy"], {"count": 1, "sum": 100.0})
        self.assertEqual(result["45"]["water"], {"count": 1, "sum": 50.5})
        self.assertEqual(result["46"], {"energy": {"count": 1, "sum": 7.0}})

    def test_cp1251_input(self):
        text = CSV.replace("OKTMO", "ОКТМО")
        result, _ = a.aggregate(self.write(text, "cp1251"), "ОКТМО", "OKVED2", 2)
        self.assertEqual(sorted(result), ["45", "46"])

    def test_cli_writes_small_outputs(self):
        src = self.write(CSV)
        out = self.dir / "out" / "urid"
        a.main([str(src), str(out), "--region-col", "OKTMO", "--okved-col", "OKVED2", "--value-col", "REVENUE"])
        payload = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(payload["stats"]["stop_list"], 2)
        md = out.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("| Энергия | 2 | 107 | 45: 100, 46: 7 | 46: 7 |", md)
        self.assertIn("отброшено по stop-листу: 2", md)

    def test_missing_column_is_reported(self):
        with self.assertRaises(SystemExit):
            a.aggregate(self.write(CSV), "OKATO", "OKVED2", 2)


if __name__ == "__main__":
    unittest.main()
