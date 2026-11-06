"""Relevance scoring of config dicts (BM25 fuzzy + exact substring)."""

import logging

from ...runtime.search_scoring import ConfigurationScoring
from ._base import _SearchBase

logger = logging.getLogger(__name__)


class ScoringMixin(ConfigurationScoring, _SearchBase):
    """Query-vs-config relevance scoring shared by the deep-search family."""

    def _score_deep_match(
        self,
        entity_id: str,
        friendly_name: str,
        fuzzy_name_score: int,
        config_match_score: int,
        query_lower: str,
        exact_match: bool,
    ) -> tuple[int, int, bool]:
        """Compute total score, threshold, and match_in_name for a deep search result.

        Returns (total_score, threshold, match_in_name).
        """
        if exact_match:
            name_exact = (
                100
                if query_lower in entity_id.lower()
                or query_lower in friendly_name.lower()
                else 0
            )
            total_score = max(name_exact, config_match_score)
            return total_score, 100, name_exact >= 100
        else:
            total_score = max(fuzzy_name_score, config_match_score)
            threshold = self.settings.fuzzy_threshold
            return total_score, threshold, fuzzy_name_score >= threshold
