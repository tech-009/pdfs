"""
Search and replace across every text element on every page.

Operates on the *current* text of each element — i.e. it sees prior
edits, so search-after-edit and replace-after-edit behave the way a
user would expect in a real editor.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .models import DocumentModel, SearchMatch


def _build_pattern(query: str, case_sensitive: bool, whole_word: bool) -> re.Pattern:
    escaped = re.escape(query)
    if whole_word:
        escaped = rf"\b{escaped}\b"
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(escaped, flags)


def current_text(document: DocumentModel, edits: Dict[str, str], element_id: str, original_text: str) -> str:
    return edits.get(element_id, original_text)


def search(
    document: DocumentModel,
    edits: Dict[str, str],
    query: str,
    case_sensitive: bool,
    whole_word: bool,
) -> List[SearchMatch]:
    if not query:
        return []
    pattern = _build_pattern(query, case_sensitive, whole_word)
    matches: List[SearchMatch] = []
    for page in document.pages:
        for el in page.text_elements:
            text = edits.get(el.id, el.text)
            if pattern.search(text):
                matches.append(SearchMatch(element_id=el.id, page=page.number, text=text))
    return matches


def replace(
    document: DocumentModel,
    edits: Dict[str, str],
    query: str,
    replacement: str,
    case_sensitive: bool,
    whole_word: bool,
    replace_all: bool,
) -> Tuple[int, List[str]]:
    if not query:
        return 0, []
    pattern = _build_pattern(query, case_sensitive, whole_word)
    updated_ids: List[str] = []

    for page in document.pages:
        for el in page.text_elements:
            text = edits.get(el.id, el.text)
            if pattern.search(text):
                new_text = pattern.sub(replacement, text)
                edits[el.id] = new_text
                updated_ids.append(el.id)
                if not replace_all:
                    return 1, updated_ids

    return len(updated_ids), updated_ids
