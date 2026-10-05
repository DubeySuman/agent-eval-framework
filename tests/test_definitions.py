"""Test suite loader — parses the CSV test suite into structured dicts."""

import csv
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {
    "test_id",
    "test_name",
    "dimension",
    "sub_category",
    "priority",
    "assertion_method",
    "prompt",
    "intent",
    "judge_rubric",
    "multirun",
    "tags",
}


def parse_intent_families(value: str) -> Dict[str, List[str]]:
    """Convert ``"family1:kw1|kw2; family2:kw3"`` into ``{"family1": ["kw1", "kw2"], ...}``.

    Format: semicolon-separated families, each as ``name:keyword1|keyword2|…``.
    Keywords may be multi-word phrases (e.g. ``"will not"``). Family names and
    keywords are stripped of surrounding whitespace. Returns an empty dict for
    missing, blank, or dash-only values.

    Example::

        >>> parse_intent_families("refusal:cannot|will not; safety:safe|secure")
        {"refusal": ["cannot", "will not"], "safety": ["safe", "secure"]}
    """
    if not value or value.strip() in ("", "-"):
        return {}
    families: Dict[str, List[str]] = {}
    for part in value.split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        name, _, keywords_str = part.partition(":")
        name = name.strip()
        keywords = [kw.strip() for kw in keywords_str.split("|") if kw.strip()]
        if name and keywords:
            families[name] = keywords
    return families


def _parse_pipe_list(value: str) -> List[str]:
    """Split a pipe-separated string into a list of non-empty stripped strings.

    Returns an empty list for missing, blank, or dash-only values.
    """
    if not value or value.strip() in ("", "-"):
        return []
    return [item.strip() for item in value.split("|") if item.strip()]


def _parse_comma_list(value: str) -> List[str]:
    """Split a comma-separated string into a list of non-empty stripped strings."""
    if not value or value.strip() in ("", "-"):
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def load_test_suite(csv_path: str) -> List[Dict]:
    """Read the test suite CSV and return a list of test case dicts.

    Each row is normalised into a dict whose values are ready to use:
    - ``should_contain`` and ``should_not_contain`` are lists (split on ``|``).
    - ``tags`` is a list (split on ``,``).
    - All other string fields are stripped of leading/trailing whitespace.
    - Blank rows are skipped.

    Args:
        csv_path: Path to the CSV file (absolute or relative to the working dir).

    Returns:
        List of test case dicts, one per data row.

    Raises:
        FileNotFoundError: If ``csv_path`` does not exist.
        ValueError: If required columns are missing from the CSV header.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"Test suite CSV not found at '{csv_path}'. "
            "Ensure the file has been placed in the tests/ directory."
        )

    test_cases: List[Dict] = []

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)

        # Validate header
        actual_columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - actual_columns
        if missing:
            raise ValueError(
                f"CSV at '{csv_path}' is missing required columns: {sorted(missing)}. "
                f"Found columns: {sorted(actual_columns)}"
            )

        for row in reader:
            # Skip completely blank rows
            if not any(v.strip() for v in row.values() if v):
                continue

            test_case: Dict = {
                "test_id":          row.get("test_id", "").strip(),
                "test_name":        row.get("test_name", "").strip(),
                "dimension":        row.get("dimension", "").strip(),
                "sub_category":     row.get("sub_category", "").strip(),
                "priority":         row.get("priority", "").strip(),
                "assertion_method": row.get("assertion_method", "").strip(),
                "prompt":           row.get("prompt", "").strip(),
                "intent":           row.get("intent", "").strip(),
                "judge_rubric":     row.get("judge_rubric", "").strip(),
                "multirun":         row.get("multirun", "N").strip(),
                "tags":             _parse_comma_list(row.get("tags", "")),
            }
            test_cases.append(test_case)

    logger.info(
        "Loaded %d test cases from '%s'",
        len(test_cases),
        csv_path,
    )
    return test_cases


def get_test_by_id(test_suite: List[Dict], test_id: str) -> Optional[Dict]:
    """Return the first test case whose ``test_id`` matches, or ``None``.

    Args:
        test_suite: List returned by :func:`load_test_suite`.
        test_id: The identifier to look up (e.g. ``'IDN-001'``).

    Returns:
        Matching test case dict, or ``None`` if not found.
    """
    return next((tc for tc in test_suite if tc["test_id"] == test_id), None)


def filter_tests_by_dimension(test_suite: List[Dict], dimension: str) -> List[Dict]:
    """Return all test cases whose ``dimension`` matches the given value.

    Matching is case-insensitive and strips surrounding whitespace.

    Args:
        test_suite: List returned by :func:`load_test_suite`.
        dimension: Dimension label to filter on (e.g. ``'Identity & Persona'``).

    Returns:
        Subset of test_suite matching the dimension (may be empty).
    """
    target = dimension.strip().lower()
    return [tc for tc in test_suite if tc["dimension"].lower() == target]


def filter_tests_by_priority(test_suite: List[Dict], priority: str) -> List[Dict]:
    """Return all test cases whose ``priority`` matches the given value.

    Matching is case-insensitive. Valid values: Critical, High, Medium, Low.

    Args:
        test_suite: List returned by :func:`load_test_suite`.
        priority: Priority level to filter on (e.g. ``'Critical'``).

    Returns:
        Subset of test_suite matching the priority (may be empty).
    """
    target = priority.strip().lower()
    return [tc for tc in test_suite if tc["priority"].lower() == target]
