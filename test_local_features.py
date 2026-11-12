import sys
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from local_nlp import correct_transcript, build_vocabulary
from gemini_service import parse_homework_locally
from datetime import date

schedule = [
    {
        'day_of_week': 0,
        'subject': 'Аксиологические основы психолого-педагогического образования (с практикумом)',
        'teacher': 'Гордин А.И.',
        'lesson_type': 'PRACTICE',
        'week_type': 'ALL'
    },
    {
        'day_of_week': 0,
        'subject': 'Нормативно-правовые и этические основы деятельности социального педагога',
        'teacher': 'Матафонова С.И.',
        'lesson_type': 'PRACTICE',
        'week_type': 'EVEN'
    },
]

test_texts = [
    'гардин задал подготовить доклад к следующей паре',
    'матафонава сказала написать реферат на эту неделю',
    'по аксилагическим основам нужно сделать конспект',
]

print("=== ТЕСТ 1: Нечеткое исправление ошибок речи (NLP) ===")
for t in test_texts:
    corrected = correct_transcript(t, schedule)
    print(f"До:    '{t}'")
    print(f"После: '{corrected}'\n")

print("=== ТЕСТ 2: Локальный парсер ДЗ (0 токенов, устойчив к опечаткам) ===")
for t in test_texts:
    corrected = correct_transcript(t, schedule)
    result = parse_homework_locally(corrected, schedule, today_date=date(2026, 11, 13))
    if result:
        tasks = result.get('tasks', [result])
        for task in tasks:
            print(f"Предмет:       {task.get('subject')}")
            print(f"Преподаватель: {task.get('teacher')}")
            print(f"Задание:       {task.get('taskText')}")
            print(f"Дедлайн:       {task.get('targetDate')}")
            print("-" * 35)
    else:
        print(f"Не удалось распознать: '{corrected}'")
