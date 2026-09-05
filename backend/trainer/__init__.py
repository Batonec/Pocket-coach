"""Backend «Pocket Coach»: всё, что импортируется.

Пакет держит хранилище (``backend_store``) и слой коуча (``coach_*``,
``prompt_builder``, ``plan_validator``, ``anthropic_client``, ``recommender``). Точка
входа HTTP-сервера — ``backend/server.py`` рядом с пакетом, скрипты таймеров —
в ``infra/jobs/``, проза для модели — в ``prompts/``, тексты баннеров — в
``resources/``; где всё это лежит, пакет знает через ``BACKEND_DIR``.
"""

from pathlib import Path

# Корень backend: локально backend/, на VPS /opt/trainer-miniapp/app. Здесь же
# лежат prompts/, resources/ и локальная data/ — всё, что код читает с диска.
BACKEND_DIR = Path(__file__).resolve().parent.parent
# Два файла, которые читает не модель, а клиент: каталог упражнений и тексты
# баннеров. Едут на прод вместе с кодом.
RESOURCES_DIR = BACKEND_DIR / "resources"
