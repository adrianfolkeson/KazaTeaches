"""Configuration. Everything that varies between machines lives here."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    # §5 cost architecture: the expensive model only where judgment quality
    # decides whether the app is worth anything (grading, rubric authoring).
    grading_model: str = os.getenv("KT_GRADING_MODEL", "claude-opus-5")
    # Item + rubric authoring. The rubric written here is what every future
    # grading of that item is matched against, so this caps grading quality.
    # Measured note in the README: opus-5 wrote better rubrics than sonnet-5 on
    # the same material — evals/gen_selfcheck.py is how you check the swap.
    generation_model: str = os.getenv("KT_GENERATION_MODEL", "claude-sonnet-5")
    # Concept extraction. §5 puts this in the cheap lane; measurement moved it.
    # See README "Why concept extraction is not on the cheap lane" — the concept
    # list decides how many items get generated, so a model that over-splits
    # costs more in generation than it saves on extraction.
    concept_model: str = os.getenv("KT_CONCEPT_MODEL", "claude-opus-5")

    database_url: str | None = os.getenv("DATABASE_URL") or None
    course_name: str = os.getenv("KT_COURSE_NAME", "Systemarkitektur")

    # Spend cap (app/budget.py). The cap is hard: at it, paid calls stop for the
    # rest of the calendar month. The target is advisory — it only changes what
    # /api/budget reports, so heavy use is visible before it is blocked.
    monthly_budget_usd: float = float(os.getenv("KT_MONTHLY_BUDGET_USD", "20"))
    monthly_target_usd: float = float(os.getenv("KT_MONTHLY_TARGET_USD", "10"))

    # One sitting. Whichever cap bites first wins. A queue longer than a sitting
    # is how spaced repetition turns into a backlog you stop opening — the items
    # that do not fit stay due and lead tomorrow's queue.
    session_max_items: int = int(os.getenv("KT_SESSION_MAX_ITEMS", "20"))
    session_max_minutes: int = int(os.getenv("KT_SESSION_MAX_MINUTES", "25"))
    # How many times one item may be asked in a single sitting. FSRS learning
    # steps assume a flashcard you re-see in seconds; here a review is a written
    # answer that costs a grading call. An item still unclear on the third
    # attempt is not going to click on the eighteenth — it belongs to tomorrow.
    session_max_per_item: int = int(os.getenv("KT_SESSION_MAX_PER_ITEM", "3"))

    # FSRS. Fuzzing jitters each interval a few percent so a batch imported on
    # the same day does not come due on the same day forever. Worth keeping on,
    # but it makes scheduling non-reproducible — tests build their own
    # unfuzzed scheduler rather than turning it off here.
    fsrs_fuzz: bool = os.getenv("KT_FSRS_FUZZ", "1") not in ("0", "false", "False")
    # An item answered correctly a few times runs to 5 years on FSRS defaults
    # (36500 days). For one course in one semester that is the same as deleting
    # it, so the ceiling is a year by default.
    fsrs_max_interval_days: int = int(os.getenv("KT_FSRS_MAX_INTERVAL_DAYS", "365"))


settings = Settings()
