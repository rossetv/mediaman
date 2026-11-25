"""The AI's part in a person's search: phrases to try, and titles for a question.

What is sent is the question, or titles and searches of the one person asking, and never
who they are. What comes back is validated, and every title is checked against TMDB.
"""

from __future__ import annotations

from .answers import AssistAnswer, Question, make_answer
from .phrases import FIXED_PHRASES, make_phrases
from .taste import Taste, words

__all__ = [
    "FIXED_PHRASES",
    "AssistAnswer",
    "Question",
    "Taste",
    "make_answer",
    "make_phrases",
    "words",
]
