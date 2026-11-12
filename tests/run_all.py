"""Запуск всех тестов: python tests/run_all.py
Каждый файл - отдельный процесс (бот читает настройки при импорте).
Реальные панели 3x-ui и Discord не используются: всё на заглушках из fakes.py."""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
failed = []
for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
    name = os.path.basename(path)
    print(f"\n=== {name}")
    r = subprocess.run([sys.executable, path], cwd=HERE, capture_output=True, text=True, timeout=600)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith(("OK", "СБОЙ"))]
    print("\n".join(lines))
    if r.returncode != 0:
        failed.append(name)
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
print("\nИтог: " + ("все тесты пройдены" if not failed else "упали " + ", ".join(failed)))
sys.exit(1 if failed else 0)
