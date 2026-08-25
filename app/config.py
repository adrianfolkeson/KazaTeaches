"""Configuration. Everything that varies between machines lives here."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    # §5 cost architecture: the expensive model only where judgment quality
    # decides whether the app is worth anything (grading, rubric authoring).
    grading_model: str = os.getenv("KT_GRADING_MODEL", "claude-opus-5")
    generation_model: str = os.getenv("KT_GENERATION_MODEL", "claude-opus-5")
    # Cheap lane: concept extraction (draft), classification, metadata.
    # If your cheap model rejects structured outputs, set KT_CHEAP_MODEL=claude-sonnet-5.
    cheap_model: str = os.getenv("KT_CHEAP_MODEL", "claude-haiku-4-5")

    database_url: str | None = os.getenv("DATABASE_URL") or None
    course_name: str = os.getenv("KT_COURSE_NAME", "Systemarkitektur")


settings = Settings()
