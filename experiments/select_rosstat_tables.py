#!/usr/bin/env python3
"""Отбор таблиц реестра открытых данных Росстата для «Цифрового двойника России».

Берёт выгрузку названий таблиц реестра (формат `[{"title": ..., "value": <ссылка на meta.csv>}]`,
приложение к issue #46) и отбирает таблицы, относящиеся к базовым потребностям:
еда, вода, жильё, медицина, образование, энергия, транспорт, а также демография.
Дополнительно отбираются труд и занятость (пара к вакансиям «Работы России»),
межотраслевые таблицы (через ОКВЭД2 покрывают воду, жильё, энергию и транспорт)
и справочники для привязки к карте (ОКТМО, ОКАТО).

Использование:
    python3 experiments/select_rosstat_tables.py [вход.json] [выход.json] [--stamp YYYY-MM-DD_HH-MM-SS]

По умолчанию читает `experiments/data/rosstat_titles_with_links.json` и пишет
`Цифровой двойник/Отбор_таблиц_Росстата_<дата>_UTC.json`.

Только стандартная библиотека Python; сеть не нужна (классификация идёт по названию и коду набора).
"""

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "experiments" / "data" / "rosstat_titles_with_links.json"
OUTPUT_DIR = ROOT / "Цифровой двойник"
ROSSTAT_INN = "7708234640"

# Порядок категорий задаёт порядок вывода. `direct` — есть ли в реестре прямые таблицы.
CATEGORIES = {
    "food": "Еда (продовольствие, сельское хозяйство)",
    "water": "Вода (водоснабжение и водоотведение)",
    "housing": "Жильё",
    "health": "Медицина и здоровье",
    "education": "Образование",
    "energy": "Энергия",
    "transport": "Транспорт",
    "demography": "Демография",
    "labour": "Труд и занятость",
    "ict": "Связь и ИКТ (дополнительно)",
    "cross_sector": "Межотраслевые таблицы (разрез по ОКВЭД2 и территории)",
    "reference": "Справочники и геопривязка (для карты)",
}

PRIORITIES = {
    1: "ядро MVP — загружать первыми",
    2: "расширение — после ядра",
    3: "детализация или архив — по запросу",
}

# Сектора, для которых в реестре нет отдельных таблиц: берутся из статрегистра (urid)
# и бухотчётности (bdboo) фильтром по кодам ОКВЭД2 (ОК 029-2014, первые знаки кода).
SECTOR_OKVED2 = {
    "food": {"okved2": ["01", "03", "10", "11"],
             "note": "01 — растениеводство и животноводство; 03 — рыболовство и рыбоводство; "
                     "10 — производство пищевых продуктов; 11 — производство напитков"},
    "water": {"okved2": ["36", "37"],
              "note": "36 — забор, очистка и распределение воды; 37 — сбор и обработка сточных вод"},
    "housing": {"okved2": ["41", "68"],
                "note": "41 — строительство зданий; 68 — операции с недвижимым имуществом "
                        "(в т.ч. 68.32 — управление жилым фондом)"},
    "health": {"okved2": ["86", "87", "88", "21"],
               "note": "86 — здравоохранение; 87 — уход с обеспечением проживания; "
                       "88 — социальные услуги без проживания; 21 — лекарственные средства"},
    "education": {"okved2": ["85"], "note": "85 — образование"},
    "energy": {"okved2": ["35"],
               "note": "35 — обеспечение электрической энергией, газом и паром "
                       "(добыча 05/06 и нефтепереработка 19 — только в агрегатах, см. stop-лист)"},
    "transport": {"okved2": ["49", "50", "51", "52", "53"],
                  "note": "49 — сухопутный и трубопроводный транспорт; 50 — водный; 51 — воздушный; "
                          "52 — складское хозяйство и вспомогательная деятельность; 53 — почта и курьеры"},
}

# Чего не хватает в реестре по каждой категории и откуда это брать (см. «Источники CSV и API.md»).
GAPS = {
    "food": "данные ВСХП-2016 устарели; текущие посевы, поголовье и производство — ЕМИСС и Минсельхоз",
    "water": "в реестре нет отдельных таблиц; организации — `urid`/`bdboo` по ОКВЭД2, "
             "показатели водоснабжения по субъектам — ЕМИСС (вне реестра)",
    "housing": "в реестре нет отдельных таблиц; ввод и площадь жилья — `nationalprojects` и ЕМИСС, "
               "застройщики и управляющие компании — `urid` по ОКВЭД2",
    "health": "есть только выборочное обследование 2021 года; мощность больниц и число врачей — ЕМИСС и Минздрав",
    "education": "уровень образования — только ВПН-2010 и микроперепись 2015; организации — `urid` (ОКВЭД2 85), "
                 "контингент — Минпросвещения и Минобрнауки",
    "energy": "в реестре нет отдельных таблиц; организации — `urid`/`bdboo` по ОКВЭД2 35, балансы — ЕМИСС",
    "transport": "в реестре нет отдельных таблиц; организации — `urid`/`bdboo` по ОКВЭД2 49–53, "
                 "дороги — `nationalprojects`, ЕМИСС, OpenStreetMap",
    "demography": "свежие ряды после ВПН-2020 — таблица `population` и ЕМИСС",
    "labour": "вакансий в реестре нет (есть только вакансии самого Росстата) — «Работа России» (trudvsem.ru/opendata)",
}

# Коды ОКВЭД2, которые исключаются из обработки целиком: оборона и безопасность.
# Это страховка от «мозаичного эффекта» (см. отчёт №4, раздел 2).
STOP_OKVED2 = {
    "25.4": "производство оружия и боеприпасов",
    "20.51": "производство взрывчатых веществ",
    "30.4": "производство военных боевых автомобилей",
    "84.22": "деятельность, связанная с обеспечением военной безопасности",
    "84.24": "деятельность по обеспечению общественного порядка и безопасности",
}

EXCLUDE_RULES = [
    (lambda t, c: "Реестр вакансий" in t,
     "вакансии в самом Росстате (кадровый набор ведомства), а не рынок труда"),
    (lambda t, c: re.match(r"(budgetexecution|procurementplans|publicprocurement|pfhdnii|"
                           r"financialreportniistat)", c),
     "бюджет, закупки и финансы Росстата"),
    (lambda t, c: re.match(r"(publiccouncil|planpubliccouncil|scientificcouncil|planscientificcouncil|"
                           r"conferencesforums|internationaltreaties|reportsrosstat|listnormativeacts|"
                           r"legalactsstatistics|publicservices|resultsinspections|realestaterosstat|"
                           r"stateprogram|indicatorsprograms|indicatorsniistat|PTOR|PPOR|okogu)", c),
     "организационная деятельность Росстата"),
    (lambda t, c: c.startswith("http"), "служебная запись (версия стандарта открытых данных)"),
]

EDUCATION_RE = re.compile(
    r"образован|бакалавр|специалист|магистр|неграмот|учен[оа]?[йя]? степен|дошкольн|общеобразоват|"
    r"послевузов|высшее|начальное общее|основное общее|среднее \(полное\)|среднее профессиональн|"
    r"начальное профессиональн|не имею образования|читать и писать|посещени[ея] дошкольн",
    re.IGNORECASE,
)
FUR_RE = re.compile(r"ПЕСЦ|СОБОЛ|НОРОК|НОРК|ЛИСИЦ|ХОРЕ|ХОРЬ|НУТРИ|ОНДАТР|БОБР")
DERIVED_PREFIX_RE = re.compile(r"^(СТРУКТУРА|РАСПРЕДЕЛЕНИЕ|УДЕЛЬНЫЙ ВЕС|ГРУППИРОВКА|ДОЛЯ)")
IKT_YEARS = {"one": "2021", "two": "2022", "three": "2023", "four": "2024", "five": "2025"}


def dataset_code(url):
    """`https://rosstat.gov.ru/opendata/7708234640-urid/meta.csv` -> `urid`."""
    if "/opendata/" not in url:
        return url
    code = url.split("/opendata/", 1)[1].split("/", 1)[0]
    # В реестре встречаются коды с задвоенным ИНН: `7708234640-7708234640-dataset2021`.
    while code.startswith(ROSSTAT_INN):
        code = code[len(ROSSTAT_INN):].lstrip("-")
    return code


def food_rule(title):
    t = title.upper()
    if "ПОГОЛОВ" in t:
        sub = "животноводство"
    elif re.search(r"ПОСЕВН|УРОЖА|МНОГОЛЕТН|ВИНОГРАДН|ЯГОДН|ТЕПЛИЦ|ПИТОМНИК|УДОБРЕН|ПЛОДОРОД|"
                   r"МЕЛИОРАЦ|ОБРАБОТАНН", t):
        sub = "растениеводство"
    elif re.search(r"РАБОТНИК|РУКОВОДИТЕЛ|ТРУДОВЫЕ|ТЕХНИК|ТРАКТОР|ЗЕРНОУБ|ПОСТРО", t):
        sub = "кадры, техника и постройки"
    else:
        sub = "хозяйства и земельные ресурсы"

    if "ОСНОВНЫЕ ИТОГИ" in t:
        prio = 1
    elif (DERIVED_PREFIX_RE.search(t) or "РУКОВОДИТЕЛ" in t or "ЭЛИТН" in t or FUR_RE.search(t)):
        prio = 3
    elif ("ВСЕХ КАТЕГОРИЙ" in t and re.search(r"ПОСЕВН|ПОГОЛОВ", t) and "КОРМОВ" not in t):
        prio = 1
    else:
        prio = 2
    return "food", sub, prio


def census_rule(title, code):
    """Микроперепись 2015, ВПН-2010, ВПН-2020 и прочая демография/образование населения."""
    is_micro = "микроперепись" in title or code.startswith("mpn2015")
    category = "education" if EDUCATION_RE.search(title) else "demography"
    if category == "education":
        sub = "уровень образования населения"
        if "дошкольн" in title.lower() and "посещ" in title.lower():
            sub = "посещение дошкольных и общеобразовательных учреждений"
    elif re.search(r"брак|разведен|разошел|разошед|вдов", title, re.IGNORECASE):
        sub = "брачное состояние"
    elif "средств к существованию" in title:
        sub = "источники средств к существованию"
    else:
        sub = "численность и возрастно-половой состав"

    if is_micro:
        sub += " (микроперепись 2015)"
        detailed = bool(re.search(r"\d+\s*[-–]\s*\d+\s*(лет|года)|\d+ лет и старше|в возрасте|"
                                  r"Возрастная группа", title))
        prio = 3 if detailed else 2
    elif "ВПН-2010" in title:
        sub += " (ВПН-2010)"
        prio = 3 if sub.startswith(("брачное", "источники")) else 2
    else:
        prio = 2
    return category, sub, prio


def classify(title, code):
    """Возвращает (категория, подкатегория, приоритет) или (None, причина_исключения, None)."""
    for rule, reason in EXCLUDE_RULES:
        if rule(title, code):
            return None, reason, None

    if code.startswith("VSHP"):
        category, sub, prio = food_rule(title)
    elif code in ("oktmo", "okato", "codingtable", "okvedva"):
        category, sub, prio = "reference", "классификаторы территорий и видов деятельности", 1
    elif code in ("okei", "numberofmunicipalities2021", "pendatapassports2021"):
        category, sub, prio = "reference", "вспомогательные справочники", 2
    elif code in ("okopf", "okfs"):
        category, sub, prio = "reference", "классификаторы для расшифровки статрегистра", 3
    elif code == "urid":
        category, sub, prio = "cross_sector", "статистический регистр (организации, ОКВЭД2, ОКТМО)", 1
    elif code == "nationalprojects":
        category, sub, prio = "cross_sector", "показатели национальных проектов", 1
    elif code == "showdatabest":
        category, sub, prio = "cross_sector", "витрина статистических данных (навигатор показателей)", 2
    elif code.startswith("bdboo"):
        category, sub, prio = "cross_sector", "бухгалтерская отчётность организаций", 2
    elif re.match(r"unemploymentrate", code):
        category, sub, prio = "labour", "безработица", 1
    elif re.match(r"employees\d", code):
        category, sub, prio = "labour", "занятые по видам экономической деятельности", 2
    elif re.match(r"(employeessubject|employeesactivity|workingconditions)", code):
        category, sub, prio = "labour", "вредные и опасные условия труда", 2
    elif code.startswith(("IKT", "itusing")):
        category, sub, prio = "ict", "использование населением ИКТ", 2
    elif code.startswith("vegetablesfruits"):
        category, sub, prio = "health", "питание (связь с категорией «еда»)", 2
    elif re.match(r"(zoh|dispans|smoking|nosmoke|health|glasses|sport|orgsport|intsport|placesforsports)",
                  code):
        sub = "спортивная инфраструктура" if code.startswith("placesforsports") else \
            "диспансеризация" if code.startswith("dispans") else "здоровье и образ жизни населения"
        category, prio = "health", 2
    elif code == "disabilitypensionaslivelihood":
        category, sub, prio = "health", "инвалидность (ВПН-2010)", 3
    elif code in ("population", "VPN2021", "VPN2021-1"):
        category, sub, prio = "demography", "численность населения (актуальные данные)", 1
    elif code in ("population2010", "dataset2021"):
        category, sub, prio = "demography", "численность населения (база сравнения)", 2
    elif (code.startswith("mpn2015") or "микроперепись" in title or "ВПН-2010" in title):
        category, sub, prio = census_rule(title, code)
    else:
        return None, "не относится к отбираемым категориям", None

    # Уточнения приоритета поверх отраслевых правил.
    if title.startswith("(Архив)"):
        prio = 3
    elif code in ("employees2025", "employeessubject2025", "healthregions-2021", "placesforsports-2021",
                  "dispans2years-2021", "IKT-twenty-twenty-five"):
        prio = 1 if category != "ict" else 2
    elif code in ("employeesactivity2022", "employeesactivity2023", "employeesactivity2024",
                  "workingconditions2022") or (category == "ict" and code != "IKT-twenty-twenty-five"):
        prio = 3
    return category, sub, prio


def geo_level(title, code, category):
    if category == "reference":
        return "справочник"
    if code == "urid" or code.startswith("bdboo"):
        return "организация (коды ОКТМО/ОКАТО в записи — агрегировать до муниципалитета/субъекта)"
    if "городских округов, муниципальных районов" in title:
        return "муниципальные образования"
    if "по субъектам" in title:
        return "субъекты РФ"
    if "по Российской Федерации" in title:
        return "Российская Федерация"
    return "уточнить по structure-файлу набора"


def period(title, code):
    if code.startswith("VSHP"):
        return "2016"
    if "микроперепись" in title or code.startswith("mpn2015"):
        return "2015"
    if "ВПН-2010" in title:
        return "2010"
    rng = re.search(r"((?:19|20)\d{2})\s*[-–]\s*((?:19|20)\d{2})", title)
    if rng:
        return f"{rng.group(1)}–{rng.group(2)}"
    years = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", title)
    if years:
        return years[0]
    m = re.match(r"IKT-twenty-twenty-(\w+)", code)
    if m and m.group(1) in IKT_YEARS:
        return IKT_YEARS[m.group(1)]
    years = re.findall(r"((?:19|20)\d{2})", code)
    return years[0] if years else None


def select(records):
    seen = set()
    selected, excluded = [], Counter()
    duplicates = 0
    for rec in records:
        url = rec["value"]
        if url in seen:
            duplicates += 1
            continue
        seen.add(url)
        title = " ".join(rec["title"].split())
        code = dataset_code(url)
        category, sub, prio = classify(title, code)
        if category is None:
            excluded[sub] += 1
            continue
        selected.append({
            "title": title,
            "value": url,
            "code": code,
            "category": category,
            "subcategory": sub,
            "priority": prio,
            "geo_level": geo_level(title, code, category),
            "period": period(title, code),
        })
    order = list(CATEGORIES)
    selected.sort(key=lambda r: (order.index(r["category"]), r["priority"], r["subcategory"], r["title"]))
    return selected, excluded, duplicates


def build_report(records, stamp):
    selected, excluded, duplicates = select(records)
    by_cat = Counter(r["category"] for r in selected)
    by_prio = Counter(r["priority"] for r in selected)
    by_cat_prio = Counter((r["category"], r["priority"]) for r in selected)
    categories = {}
    for key, name in CATEGORIES.items():
        entry = {
            "name": name,
            "tables_total": by_cat.get(key, 0),
            "tables_by_priority": {str(p): by_cat_prio.get((key, p), 0) for p in PRIORITIES},
        }
        if key in SECTOR_OKVED2:
            entry["via_okved2"] = SECTOR_OKVED2[key]
        if key in GAPS:
            entry["gap"] = GAPS[key]
        categories[key] = entry
    return {
        "name": f"Отбор таблиц реестра открытых данных Росстата для «Цифрового двойника России» ({stamp} UTC)",
        "created_utc": stamp,
        "issue": "https://github.com/PavelChurkin/resource-based-economy-Article/issues/46",
        "report": f"Отчёт_анализа_{stamp}_UTC.md",
        "source": {
            "file": "experiments/data/rosstat_titles_with_links.json",
            "origin": "приложение к issue #46 (titles_with_links.json)",
            "records_total": len(records),
            "duplicate_urls_skipped": duplicates,
            "unique_datasets": len(records) - duplicates,
        },
        "method": ("Классификация по названию и коду набора (часть URL после ИНН Росстата) скриптом "
                   "experiments/select_rosstat_tables.py. Ссылка `value` ведёт на meta.csv набора "
                   "(стандарт открытых данных 3.0): в нём — ссылки на актуальные data-*.csv и structure-*.csv."),
        "priorities": {str(k): v for k, v in PRIORITIES.items()},
        "summary": {
            "selected": len(selected),
            "excluded": sum(excluded.values()),
            "selected_by_priority": {str(p): by_prio.get(p, 0) for p in PRIORITIES},
            "excluded_by_reason": dict(excluded.most_common()),
        },
        "categories": categories,
        "stop_okved2": STOP_OKVED2,
        "tables": selected,
    }


def main(argv):
    args = [a for a in argv if not a.startswith("--stamp")]
    stamp = None
    for i, a in enumerate(argv):
        if a.startswith("--stamp"):
            stamp = a.split("=", 1)[1] if "=" in a else argv[i + 1]
            if "=" not in a:
                args.remove(stamp)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    src = Path(args[0]) if args else DEFAULT_INPUT
    dst = Path(args[1]) if len(args) > 1 else OUTPUT_DIR / f"Отбор_таблиц_Росстата_{stamp}_UTC.json"

    records = json.loads(src.read_text(encoding="utf-8"))
    report = build_report(records, stamp)
    dst.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    s = report["summary"]
    print(f"Записей: {len(records)}, отобрано: {s['selected']}, исключено: {s['excluded']}, "
          f"дублей: {report['source']['duplicate_urls_skipped']}")
    for key, cat in report["categories"].items():
        print(f"  {cat['name']}: {cat['tables_total']} {cat['tables_by_priority']}")
    print(f"Файл: {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
