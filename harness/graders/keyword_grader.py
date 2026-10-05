"""Keyword-based grader — deterministic string-presence checks."""

import logging
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


class KeywordGrader:
    """Deprecated — all tests now use the LLM judge (JudgeGrader).

    Retained for backward compatibility. ``grade()`` returns a neutral no-op
    result; helper methods (``grade_contains``, ``grade_not_contains``) remain
    available for callers that import them directly.
    """

    def grade(self, response_text: str, test_case: Dict) -> Dict:
        """Deprecated no-op. All tests are now evaluated by JudgeGrader.

        Returns a neutral passing result so any stale caller does not fail.
        """
        return {
            "passed": True,
            "reason": "Deprecated: all tests now use LLM judge.",
            "details": {"status": "JUDGE_ONLY"},
        }

    # ------------------------------------------------------------------
    # Intent-based grading
    # ------------------------------------------------------------------

    def _grade_by_intent(self, response_text: str, test_case: Dict) -> Dict:
        """Grade by semantic intent families.

        For each family in ``required_intent_families``, the response must
        contain at least one keyword from that family (case-insensitive
        substring match). ``should_not_contain`` is always strictly enforced.

        Args:
            response_text: Agent response to evaluate.
            test_case: Must contain ``required_intent_families`` (dict mapping
                family name → list of keywords) and optionally
                ``should_not_contain`` (list of forbidden strings).

        Returns:
            ::

                {
                    "passed": bool,
                    "reason": str,
                    "details": {
                        "matched_families": {family_name: [matched_keywords]},
                        "missing_families": [family_names_with_no_match],
                        "found_forbidden": [forbidden_keywords_found],
                    },
                }
        """
        required_families: Dict[str, List[str]] = (
            test_case.get("required_intent_families") or {}
        )
        should_not_contain: List[str] = test_case.get("should_not_contain") or []

        lowered = response_text.lower()
        matched_families: Dict[str, List[str]] = {}
        missing_families: List[str] = []

        for family_name, keywords in required_families.items():
            hits = [kw for kw in keywords if kw.lower() in lowered]
            if hits:
                matched_families[family_name] = hits
            else:
                missing_families.append(family_name)

        intent_ok = len(missing_families) == 0
        not_contains_ok, found_forbidden = self.grade_not_contains(
            response_text, should_not_contain
        )

        passed = intent_ok and not_contains_ok

        parts: List[str] = []
        if missing_families:
            parts.append(f"Missing intent families: {', '.join(missing_families)}")
        if found_forbidden:
            quoted = ", ".join(f"'{k}'" for k in found_forbidden)
            parts.append(f"Forbidden keyword(s) found: {quoted}")
        reason = "; ".join(parts) if parts else "All intent families matched"

        logger.debug(
            "KeywordGrader [intent]: test_id=%s passed=%s reason=%s",
            test_case.get("test_id", "unknown"),
            passed,
            reason,
        )
        return {
            "passed": passed,
            "reason": reason,
            "details": {
                "matched_families": matched_families,
                "missing_families": missing_families,
                "found_forbidden": found_forbidden,
            },
        }

    # ------------------------------------------------------------------
    # Keyword-based grading (original logic — backward compatible)
    # ------------------------------------------------------------------

    def _grade_by_keywords(self, response_text: str, test_case: Dict) -> Dict:
        """Grade by strict keyword presence (original behavior).

        Args:
            response_text: Agent response to search within.
            test_case: Must contain ``should_contain`` and
                ``should_not_contain`` lists.

        Returns:
            ::

                {
                    "passed": bool,
                    "reason": str,
                    "details": {
                        "found_required": list[str],
                        "missing_required": list[str],
                        "found_forbidden": list[str],
                    },
                }
        """
        should_contain: List[str] = test_case.get("should_contain") or []
        should_not_contain: List[str] = test_case.get("should_not_contain") or []

        contains_ok, found_required, missing_required = self.grade_contains(
            response_text, should_contain
        )
        not_contains_ok, found_forbidden = self.grade_not_contains(
            response_text, should_not_contain
        )

        passed = contains_ok and not_contains_ok
        reason = self._build_reason(missing_required, found_forbidden)

        logger.debug(
            "KeywordGrader [keyword]: test_id=%s passed=%s reason=%s",
            test_case.get("test_id", "unknown"),
            passed,
            reason,
        )
        return {
            "passed": passed,
            "reason": reason,
            "details": {
                "found_required": found_required,
                "missing_required": missing_required,
                "found_forbidden": found_forbidden,
            },
        }

    # ------------------------------------------------------------------
    # Shared helpers (public so callers can use them directly)
    # ------------------------------------------------------------------

    def grade_contains(
        self,
        response_text: str,
        should_contain: List[str],
    ) -> Tuple[bool, List[str], List[str]]:
        """Check that every keyword in ``should_contain`` appears in the response.

        Args:
            response_text: Agent response to search within.
            should_contain: Keywords that must all be present (case-insensitive).

        Returns:
            Tuple of ``(all_found, found, missing)`` where:

            - ``all_found``: True if every keyword was found.
            - ``found``: Keywords that were present.
            - ``missing``: Keywords that were absent.
        """
        lowered = response_text.lower()
        found: List[str] = []
        missing: List[str] = []

        for keyword in should_contain:
            if not keyword:
                continue
            if keyword.lower() in lowered:
                found.append(keyword)
            else:
                missing.append(keyword)

        all_found = len(missing) == 0
        return all_found, found, missing

    def grade_not_contains(
        self,
        response_text: str,
        should_not_contain: List[str],
    ) -> Tuple[bool, List[str]]:
        """Check that none of the keywords in ``should_not_contain`` appear.

        Args:
            response_text: Agent response to search within.
            should_not_contain: Keywords that must NOT be present (case-insensitive).

        Returns:
            Tuple of ``(none_found, found_forbidden)`` where:

            - ``none_found``: True if no forbidden keyword was found.
            - ``found_forbidden``: Forbidden keywords that were present.
        """
        lowered = response_text.lower()
        found_forbidden: List[str] = []

        for keyword in should_not_contain:
            if not keyword:
                continue
            if keyword.lower() in lowered:
                found_forbidden.append(keyword)

        none_found = len(found_forbidden) == 0
        return none_found, found_forbidden

    @staticmethod
    def _build_reason(missing_required: List[str], found_forbidden: List[str]) -> str:
        """Compose a human-readable pass/fail reason string."""
        parts: List[str] = []
        if missing_required:
            quoted = ", ".join(f"'{k}'" for k in missing_required)
            parts.append(f"Missing required keyword(s): {quoted}")
        if found_forbidden:
            quoted = ", ".join(f"'{k}'" for k in found_forbidden)
            parts.append(f"Forbidden keyword(s) found: {quoted}")
        return "; ".join(parts) if parts else "All keyword checks passed"
