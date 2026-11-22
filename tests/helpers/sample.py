# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Where the Sample family's travellers file for the tests lives."""

from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "sample" / "config"
TRAVELLERS = CONFIG_DIR / "travellers.yaml"
