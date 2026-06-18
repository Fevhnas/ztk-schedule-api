#!/usr/bin/env python3
"""
ZTK Schedule fetcher for Caelestia dashboard & Android Widget.
Automated multi-course and multi-group API generator.
"""

import os
import sys
import json
import re
import io
import time
import shutil
import requests
import pdfplumber
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone

# ── Config ──────────────────────────────────────────────────────────────────
MAIN_URL_TEMPLATE = "https://ztk.org.ua/files/{course_encoded}.pdf"
SUBS_URL_TEMPLATE = "https://ztk.org.ua/files/{date}.pdf"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache"
}

DAY_NAMES = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]

PDF_TABLE_SETTINGS = {
    "text_y_tolerance": 1.5,
    "text_x_tolerance": 2
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def get_day_parity(date: datetime) -> str:
    return "odd" if date.day % 2 != 0 else "even"

def format_date(date: datetime) -> str:
    return date.strftime("%d.%m.%Y")

def day_name(date: datetime) -> str:
    return DAY_NAMES[date.weekday()]

# ── Network Fetchers ─────────────────────────────────────────────────────────

def fetch_main_pdf(course: int) -> bytes | None:
    course_text = f"Розклад занять {course} курс"
    course_encoded = urllib.parse.quote(course_text)
    base_url = MAIN_URL_TEMPLATE.format(course_encoded=course_encoded)
    url = f"{base_url}?t={int(time.time())}"
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200 and b'%PDF' in r.content[:8]:
                return r.content
            else:
                print(f"fetch_main_pdf({course}) attempt {attempt+1} status: {r.status_code}")
        except requests.RequestException as e:
            print(f"fetch_main_pdf({course}) attempt {attempt+1} exception: {e}")
        if attempt < 1:
            time.sleep(1)
    return None

def fetch_subs_pdf(date: datetime) -> bytes | None:
    base_url = SUBS_URL_TEMPLATE.format(date=format_date(date))
    url = f"{base_url}?t={int(time.time())}"
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200 and b'%PDF' in r.content[:8]:
                return r.content
        except requests.RequestException:
            pass
        if attempt < 1:
            time.sleep(1)
    return None

# ── Group Discovery ──────────────────────────────────────────────────────────

def get_table_groups(table) -> list:
    fmt = detect_pdf_format(table)
    groups = set()
    group_regex = r'^[А-ЯЄІЇа-яєіїA-Z]{1,4}\d+[А-ЯЄІЇa-zA-Zа-яєіїa-z]?$'
    
    if fmt == 'wide':
        if len(table) >= 3 and table[0][2]:
            tokens = str(table[0][2]).replace('\n', ' ').split()
            for t in tokens:
                clean = t.strip().replace(' ', '')
                if re.match(group_regex, clean) and clean != 'ирап':
                    groups.add(clean)
    else:
        for row in table[:5]:
            for cell in row:
                if cell:
                    tokens = str(cell).replace('\n', ' ').split()
                    for t in tokens:
                        clean = t.strip().replace(' ', '')
                        if re.match(group_regex, clean):
                            groups.add(clean)
    return sorted(list(groups))

# ── Substitutions parser ─────────────────────────────────────────────────────

def parse_subs_pdf(pdf_bytes: bytes, group: str) -> dict:
    result = {}
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables(table_settings=PDF_TABLE_SETTINGS):
                    current_groups = []
                    for row in table:
                        if not row or all(c is None for c in row):
                            continue

                        group_col, para_col, subject, teacher, room = (row + [None]*5)[:5]

                        if group_col == 'Група' or subject == 'Дисципліна':
                            continue

                        if group_col is not None:
                            clean = group_col.replace('\n', '').replace(' ', '')
                            current_groups = clean.split(',')

                        if not para_col or not current_groups:
                            continue

                        is_cancelled = subject and '---' in subject
                        if is_cancelled:
                            subject, teacher, room = "ВІДМІНЕНО", "-", "-"
                        else:
                            if subject: subject = subject.replace('\n', ' ').strip()
                            if teacher: teacher = teacher.replace('\n', ' ').strip()
                            if room:    room    = room.replace('\n', ' ').strip()

                        para_col = para_col.strip() if para_col else para_col
                        if group in current_groups:
                            result[para_col] = {
                                "subject": subject,
                                "teacher": teacher,
                                "room": room,
                                "cancelled": is_cancelled
                            }
    finally:
        os.unlink(tmp_path)

    return result

# ── Day name detection ────────────────────────────────────────────────────────

def get_normal_day(text):
    if not text: return None
    t = text.replace('\n', '').replace(' ', '')
    if 'коліденоП' in t: return 'Понеділок'
    if 'коротвіВ' in t: return 'Вівторок'
    if 'адереС' in t:   return 'Середа'
    if 'ревтеЧ' in t:   return 'Четвер'
    if 'яцинтяП' in t or "яцинтя'П" in t or 'яцинтя\u2019П' in t: return "П'ятниця"
    return None

# ── Format detection ──────────────────────────────────────────────────────────

def detect_pdf_format(table) -> str:
    if not table or len(table) < 2:
        return 'narrow'
    row0 = table[0]
    if row0[2] and len(str(row0[2])) > 50 and re.search(r'[А-ЯЄІЇа-яєіїA-Z]{1,4}\d+', str(row0[2])):
        return 'wide'
    return 'narrow'

def get_group_col_narrow(table, group: str) -> int:
    for row in table[:5]:
        for idx, cell in enumerate(row):
            if cell and group in cell:
                return idx
    return -1

def get_group_col_wide(table, group: str) -> int:
    if not table or len(table) < 3:
        return -1

    groups_str = str(table[0][2]).replace('\n', ' ')
    tokens = groups_str.split()
    group_names = [t for t in tokens
                   if re.match(r'^[А-ЯЄІЇа-яєіїA-Z]{1,4}\d+[А-ЯЄІЇa-zA-Zа-яєіїa-z]?$', t)
                   and t != 'ирап']

    row1 = table[1]
    row2 = table[2]
    para_cols = []
    for i in range(len(row1)):
        v1 = str(row1[i]).strip() if row1[i] else ''
        v2 = str(row2[i]).strip() if row2[i] else ''
        if v1 == '1' and v2 == '2':
            para_cols.append(i)

    for grp, pcol in zip(group_names, para_cols):
        if grp == group:
            return pcol

    return -1

# ── Core lesson builder ───────────────────────────────────────────────────────

def apply_second_row(schedule, current_day, current_para_num, subject, teacher, room):
    existing = schedule[current_day][current_para_num]
    if existing.get("type") != "regular":
        return
    existing_subject = existing.get("subject")
    if subject is None:
        return
    elif existing_subject is None:
        schedule[current_day][current_para_num] = {
            "type": "alternating",
            "even": {"subject": None, "teacher": None, "room": None},
            "odd":  {"subject": subject, "teacher": teacher, "room": room}
        }
    else:
        odd_room = room if room else existing.get("room")
        schedule[current_day][current_para_num] = {
            "type": "alternating",
            "even": {
                "subject": existing_subject,
                "teacher": existing.get("teacher"),
                "room":    existing.get("room")
            },
            "odd": {
                "subject": subject,
                "teacher": teacher,
                "room":    odd_room
            }
        }

def clean_schedule(schedule):
    for day in list(schedule):
        for p in list(schedule[day]):
            d = schedule[day][p]
            if d["type"] == "regular" and not d.get("subject"):
                del schedule[day][p]
            elif d["type"] == "alternating" and not d["even"].get("subject") and not d["odd"].get("subject"):
                del schedule[day][p]

# ── Main schedule parser ──────────────────────────────────────────────────────

def parse_main_schedule(pdf_bytes: bytes, group: str) -> dict:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        tables = pdf.pages[0].extract_tables(table_settings=PDF_TABLE_SETTINGS)
        if not tables:
            return {}

        table = tables[0]
        fmt = detect_pdf_format(table)

        if fmt == 'narrow':
            group_col_index = get_group_col_narrow(table, group)
            if group_col_index == -1:
                return {}
            schedule = _parse_narrow(table, group_col_index)
        else:
            para_col = get_group_col_wide(table, group)
            if para_col == -1:
                return {}
            schedule = _parse_wide(table, para_col)

    clean_schedule(schedule)
    return schedule


def _parse_narrow(table, group_col_index: int) -> dict:
    schedule = {}
    current_day = None
    current_para_num = None

    for row in table:
        detected = get_normal_day(row[0])
        if detected:
            current_day = detected
            schedule.setdefault(current_day, {})

        if not current_day:
            continue

        para_idx    = group_col_index - 1
        subject_idx = group_col_index
        teacher_idx = group_col_index + 1
        room_idx    = group_col_index + 2

        if room_idx >= len(row):
            continue

        raw_para    = row[para_idx]
        raw_subject = row[subject_idx]
        raw_teacher = row[teacher_idx]
        raw_room    = row[room_idx]

        para_num = str(raw_para).strip() if raw_para else None
        subject  = raw_subject.replace('\n', ' ').strip() if isinstance(raw_subject, str) else None
        teacher  = raw_teacher.replace('\n', ' ').strip() if isinstance(raw_teacher, str) else None
        room     = raw_room.replace('\n', ' ').strip() if isinstance(raw_room, str) else None

        if not subject: subject = None
        if not teacher: teacher = None
        if not room:    room    = None

        if para_num and para_num.isdigit():
            current_para_num = para_num
            schedule[current_day][current_para_num] = {
                "type": "regular",
                "subject": subject,
                "teacher": teacher,
                "room": room
            }
        elif not para_num and current_para_num and current_para_num in schedule[current_day]:
            if raw_subject is not None:
                apply_second_row(schedule, current_day, current_para_num, subject, teacher, room)

    return schedule


def _parse_wide(table, para_col: int) -> dict:
    subject_col = para_col + 1
    teacher_col = para_col + 2
    room_col    = para_col + 3

    schedule = {}
    current_day = None
    current_para_num = None

    for row in table:
        detected = get_normal_day(row[0])
        if detected:
            current_day = detected
            schedule.setdefault(current_day, {})

        if not current_day:
            continue

        if room_col >= len(row):
            continue

        raw_para    = row[para_col]
        raw_subject = row[subject_col]
        raw_teacher = row[teacher_col]
        raw_room    = row[room_col]

        para_num = str(raw_para).strip() if raw_para else None
        subject  = raw_subject.replace('\n', ' ').strip() if isinstance(raw_subject, str) else None
        teacher  = raw_teacher.replace('\n', ' ').strip() if isinstance(raw_teacher, str) else None
        room     = raw_room.replace('\n', ' ').strip() if isinstance(raw_room, str) else None

        if not subject: subject = None
        if not teacher: teacher = None
        if not room:    room    = None

        if para_num and para_num.isdigit():
            current_para_num = para_num
            schedule[current_day][current_para_num] = {
                "type": "regular",
                "subject": subject,
                "teacher": teacher,
                "room": room
            }
        elif not para_num and current_para_num and current_para_num in schedule[current_day]:
            if raw_subject is not None:
                apply_second_row(schedule, current_day, current_para_num, subject, teacher, room)

    return schedule

# ── Merge: apply subs on top of a day's lessons ──────────────────────────────

PARA_ROMAN = {"1": "I", "2": "II", "3": "III", "4": "IV", "5": "V", "6": "VI"}
PARA_ARABIC = {v: k for k, v in PARA_ROMAN.items()}

def expand_para_range(roman_key: str) -> list[str]:
    parts = roman_key.strip().split('-')
    if len(parts) == 2:
        start = PARA_ARABIC.get(parts[0].strip())
        end   = PARA_ARABIC.get(parts[1].strip())
        if start and end:
            return [str(i) for i in range(int(start), int(end) + 1)]
    single = PARA_ARABIC.get(roman_key.strip())
    return [single] if single else []

def resolve_lesson(lesson: dict, parity: str) -> dict | None:
    if lesson["type"] == "regular":
        if not lesson.get("subject"):
            return None
        return {
            "subject": lesson["subject"],
            "teacher": lesson["teacher"],
            "room":    lesson["room"]
        }
    else:
        slot = lesson["even"] if parity == "even" else lesson["odd"]
        if not slot.get("subject"):
            return None
        return {
            "subject": slot["subject"],
            "teacher": slot["teacher"],
            "room":    slot["room"]
        }

def build_day_lessons(day_schedule: dict, subs: dict, parity: str) -> list:
    lessons = {}

    for num_str, lesson in day_schedule.items():
        resolved = resolve_lesson(lesson, parity)
        if resolved:
            lessons[int(num_str)] = {
                "para":        int(num_str),
                "subject":     resolved["subject"],
                "teacher":     resolved["teacher"],
                "room":        resolved["room"],
                "cancelled":   False,
                "substituted": False
            }

    for roman, sub in subs.items():
        for arabic in expand_para_range(roman):
            num = int(arabic)
            if sub["cancelled"]:
                if num in lessons:
                    lessons[num]["cancelled"] = True
                    lessons[num]["substituted"] = True
                else:
                    lessons[num] = {
                        "para":        num,
                        "subject":     None,
                        "teacher":     None,
                        "room":        None,
                        "cancelled":   True,
                        "substituted": True
                    }
            else:
                lessons[num] = {
                    "para":        num,
                    "subject":     sub["subject"],
                    "teacher":     sub["teacher"],
                    "room":        sub["room"],
                    "cancelled":   False,
                    "substituted": True
                }

    return sorted(lessons.values(), key=lambda x: x["para"])

def get_kyiv_time() -> datetime:
    utc_now = datetime.now(timezone.utc)
    if 4 <= utc_now.month <= 10:
        offset = 3
    elif utc_now.month == 3:
        last_sunday = 31 - (datetime(utc_now.year, 3, 31).weekday() + 1) % 7
        offset = 3 if utc_now.day >= last_sunday else 2
    elif utc_now.month == 10:
        last_sunday = 31 - (datetime(utc_now.year, 10, 31).weekday() + 1) % 7
        offset = 2 if utc_now.day >= last_sunday else 3
    else:
        offset = 2
    return utc_now.astimezone(timezone(timedelta(hours=offset)))

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if os.path.exists("api"):
        shutil.rmtree("api")
    os.makedirs("api", exist_ok=True)

    now = get_kyiv_time()
    parity = get_day_parity(now)

    print("Fetching substitutions...")
    today_subs_pdf = fetch_subs_pdf(now)
    tomorrow = now + timedelta(days=1)
    tomorrow_subs_pdf = fetch_subs_pdf(tomorrow)
    tomorrow_date_str = format_date(tomorrow)
    tomorrow_missing = tomorrow_subs_pdf is None

    today_name    = day_name(now)
    tomorrow_name = day_name(tomorrow)
    tomorrow_parity = get_day_parity(tomorrow)

    course_groups_map = {}

    # Сканируем все 4 курса
    for course in [1, 2, 3, 4]:
        print(f"Processing Course {course}...")
        pdf_bytes = fetch_main_pdf(course)
        if not pdf_bytes:
            print(f"Skipping Course {course}: PDF not found on website.")
            continue

        # Вытаскиваем список групп из этого курса
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            tables = pdf.pages[0].extract_tables(table_settings=PDF_TABLE_SETTINGS)
            if not tables:
                continue
            groups = get_table_groups(tables[0])

        if not groups:
            print(f"No groups found for course {course}.")
            continue

        course_groups_map[str(course)] = groups

        # Генерируем расписание для каждой найденной группы
        for group in groups:
            main_sched = parse_main_schedule(pdf_bytes, group)
            if not main_sched:
                continue

            today_subs = parse_subs_pdf(today_subs_pdf, group) if today_subs_pdf else {}
            tomorrow_subs = parse_subs_pdf(tomorrow_subs_pdf, group) if tomorrow_subs_pdf else {}

            today_lessons = build_day_lessons(main_sched.get(today_name, {}), today_subs, parity)
            tomorrow_lessons = build_day_lessons(main_sched.get(tomorrow_name, {}), tomorrow_subs, tomorrow_parity)

            output = {
                "group":                 group,
                "course":                course,
                "day_parity":            parity,
                "tomorrow_parity":       tomorrow_parity,
                "tomorrow_date":         tomorrow_date_str,
                "subs_tomorrow_missing": tomorrow_missing,
                "today":                 today_name,
                "today_lessons":         today_lessons,
                "tomorrow":              tomorrow_name,
                "tomorrow_lessons":      tomorrow_lessons
            }

            # Сохраняем индивидуальный файл группы
            with open(f"api/{group}.json", "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2, ensure_ascii=False)

    # Сохраняем индексный файл со структурой Курс -> Группы
    with open("api/groups.json", "w", encoding="utf-8") as f:
        json.dump(course_groups_map, f, indent=2, ensure_ascii=False)

    print("Success! All JSONs generated in 'api/' directory.")

if __name__ == "__main__":
    main()
