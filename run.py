"""Первый и последующие запуски: python run.py [параметры cik_export.py]."""
from pathlib import Path
import os
import subprocess
import sys
import venv


def main():
    if sys.version_info < (3, 10):
        print('[ERROR] Требуется Python 3.10 или новее.', file=sys.stderr)
        return 1
    base = Path(__file__).resolve().parent
    environment = base / '.venv'
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    requirements = base / 'requirements.txt'
    installed = environment / 'requirements.installed'
    try:
        if not python.is_file():
            print('[INFO] Создание окружения .venv…', flush=True)
            venv.create(environment, with_pip=True)
        expected = requirements.read_bytes()
        if not installed.is_file() or installed.read_bytes() != expected:
            print('[INFO] Установка зависимостей…', flush=True)
            subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(requirements)],
                           cwd=base, check=True)
            installed.write_bytes(expected)
        return subprocess.call([str(python), str(base / 'cik_export.py'), *sys.argv[1:]], cwd=base)
    except subprocess.CalledProcessError as exc:
        print('[ERROR] Не удалось установить зависимости. Повторите запуск после устранения ошибки выше.',
              file=sys.stderr)
        return exc.returncode
    except OSError as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
