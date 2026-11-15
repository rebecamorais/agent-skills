"""The skill's own instructions follow its wording rules, and the word list stays in sync."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import helpers  # noqa: F401
import engcore

SKILL = Path(__file__).resolve().parent.parent


def without_word_list(text: str) -> str:
    """Drop lines that quote the list on purpose (the list itself and the 'Instead of' table)."""
    keep = []
    for line in text.splitlines():
        quoted = re.findall(r"`([^`]+)`", line)
        if sum(word in quoted for word in engcore.BANNED_WORDS) >= 3 or line.startswith("| \"") or "(overdue" in line:
            continue
        keep.append(line)
    return "\n".join(keep)


class SkillTextTest(unittest.TestCase):
    def test_word_list_matches_principles(self):
        text = (SKILL / "references" / "principles.md").read_text()
        listed = re.findall(r"`([^`]+)`", next(line for line in text.splitlines() if "`overdue`" in line))
        self.assertEqual(tuple(listed), engcore.BANNED_WORDS)

    def test_instructions_use_no_pressure_words(self):
        for path in [SKILL / "SKILL.md", SKILL / "docs.md", *sorted((SKILL / "references").glob("*.md"))]:
            with self.subTest(path=path.name):
                self.assertEqual(engcore.banned_words_in(without_word_list(path.read_text())), [])

    def test_instructions_are_gender_neutral(self):
        for path in [SKILL / "SKILL.md", SKILL / "docs.md", *sorted((SKILL / "references").glob("*.md"))]:
            with self.subTest(path=path.name):
                self.assertEqual(re.findall(r"\b(she|her|hers|herself)\b", path.read_text(), re.I), [])


if __name__ == "__main__":
    unittest.main()
