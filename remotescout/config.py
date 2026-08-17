import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


SCORING_BUDGET_DEFAULT = 15


def load_config():
    return {
        "DATABASE_PATH": os.environ.get(
            "REMOTESCOUT_DATABASE_PATH",
            str(BASE_DIR / "instance" / "remotescout.db"),
        ),
        "RESUME_PATH": os.environ.get(
            "REMOTESCOUT_RESUME_PATH",
            str(BASE_DIR / "docs" / "Michael-Buckingham-Resume-Infrastructure-Delivery-Director.pdf"),
        ),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        "RECOMMENDATION_THRESHOLD": float(
            os.environ.get("REMOTESCOUT_RECOMMENDATION_THRESHOLD", "70")
        ),
        "SCORING_BUDGET": int(
            os.environ.get("REMOTESCOUT_SCORING_BUDGET", str(SCORING_BUDGET_DEFAULT))
        ),
    }
