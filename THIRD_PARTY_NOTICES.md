# Сторонние компоненты

Версии Python-пакетов зафиксированы в `uv.lock`. В однофайловой сборке тексты лицензий находятся внутри EXE и автоматически распаковываются во временную папку PyInstaller (`licenses`). Документация и лицензии MKVToolNix извлекаются вместе с утилитами в `%LOCALAPPDATA%\SubsRenamer\tools\mkvtoolnix-<версия архива>\doc` при первой работе с MKV.

| Компонент | Лицензия / источник |
| --- | --- |
| Python | PSF License — https://www.python.org/downloads/source/ |
| PySide6 / Shiboken6 / Qt | LGPLv3 / GPLv3 / коммерческая; используются динамические библиотеки, распаковываемые PyInstaller. Исходники приложения, lock-файл и сценарий сборки позволяют пересобрать EXE с изменёнными библиотеками. Лицензии входят в поставку пакетов. Исходники Qt: https://download.qt.io/archive/qt/ ; Qt for Python: https://download.qt.io/official_releases/QtForPython/ |
| google-genai | Apache-2.0 — https://github.com/googleapis/python-genai |
| Pydantic / pydantic-core | MIT — https://github.com/pydantic/pydantic |
| HTTPX | BSD-3-Clause — https://github.com/encode/httpx |
| socksio | MIT — https://github.com/sethmlarson/socksio |
| python-dotenv | BSD-3-Clause — https://github.com/theskumar/python-dotenv |
| MKVToolNix 103.0 | GPL-2.0-or-later и лицензии включённых компонентов; https://mkvtoolnix.download/ |
| PyInstaller | GPL с исключением для загрузчика; https://pyinstaller.org/en/stable/license.html |

MKVToolNix вызывается отдельным процессом. Включены неизменённые `mkvmerge.exe` и `mkvextract.exe` из официальной поставки Windows x64. Соответствующие исходники MKVToolNix включены внутрь `mkvtoolnix.zip` в EXE и после первой работы с MKV доступны в `source/mkvtoolnix-103.0.tar.xz` рядом с распакованными утилитами. Официальный источник: https://mkvtoolnix.download/sources/mkvtoolnix-103.0.tar.xz. Версия, URL и SHA-256 бинарного архива записаны в `SOURCE-AND-VERSION.txt`. Документация и уведомления из официальной поставки сохраняются.

Исходники приложения и сценарий повторной сборки доступны в проекте. Перед распространением модифицированной сборки сохраняйте уведомления и лицензии её зависимостей. Приватная сборка содержит API-ключ владельца; исходники не содержат его значения.
