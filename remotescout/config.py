import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


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
        "RECOMMENDATION_THRESHOLD": float(
            os.environ.get("REMOTESCOUT_RECOMMENDATION_THRESHOLD", "70")
        ),
    }
