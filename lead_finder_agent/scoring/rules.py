"""Declarative scoring rules loaded from configuration.

Rules are data, not code: each rule has a ``when`` mapping of signal -> expected
value. A rule fires when *all* of its conditions match, which keeps them easy to
write and review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from lead_finder_agent.config.loader import load_config_file, load_packaged_data, merge_dicts
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("scoring.rules")


@dataclass
class ScoringRule:
    """A single scoring rule."""

    id: str
    points: int
    description: str = ""
    when: Dict[str, Any] = field(default_factory=dict)
    category: str = "general"

    def matches(self, signals: Mapping[str, Any]) -> bool:
        """True when every condition in ``when`` holds for ``signals``."""
        for key, expected in self.when.items():
            actual = signals.get(key)
            if isinstance(expected, bool):
                if bool(actual) is not expected:
                    return False
            elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
                if actual is None:
                    return False
                try:
                    if float(actual) < float(expected):
                        return False
                except (TypeError, ValueError):
                    return False
            elif actual != expected:
                return False
        return True

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], category: str = "general") -> "ScoringRule":
        if "id" not in data:
            raise ValueError(f"Scoring rule is missing an 'id': {data!r}")
        if "points" not in data:
            raise ValueError(f"Scoring rule {data['id']!r} is missing 'points'")
        return cls(
            id=str(data["id"]),
            points=int(data["points"]),
            description=str(data.get("description") or ""),
            when=dict(data.get("when") or {}),
            category=str(data.get("category") or category),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "points": self.points,
            "description": self.description,
            "when": dict(self.when),
            "category": self.category,
        }


@dataclass
class ScoringRules:
    """A complete, validated scoring rule set."""

    rules: List[ScoringRule] = field(default_factory=list)
    thresholds: Dict[str, Any] = field(default_factory=dict)
    confidence_thresholds: Dict[str, int] = field(default_factory=dict)
    score_min: int = 0
    score_max: int = 100
    version: int = 1
    name: str = "default"
    source: str = "defaults"

    def rule(self, rule_id: str) -> Optional[ScoringRule]:
        for candidate in self.rules:
            if candidate.id == rule_id:
                return candidate
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "thresholds": dict(self.thresholds),
            "confidence": dict(self.confidence_thresholds),
            "rules": [r.to_dict() for r in self.rules],
            "score_min": self.score_min,
            "score_max": self.score_max,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: str = "dict") -> "ScoringRules":
        payload = dict(data or {})
        raw_rules = payload.get("rules") or []
        if not isinstance(raw_rules, Sequence) or isinstance(raw_rules, (str, bytes)):
            raise ValueError("'rules' must be a list")
        rules = [ScoringRule.from_dict(item) for item in raw_rules]
        ids = [rule.id for rule in rules]
        duplicates = {rid for rid in ids if ids.count(rid) > 1}
        if duplicates:
            raise ValueError(f"Duplicate scoring rule ids: {sorted(duplicates)}")
        return cls(
            rules=rules,
            thresholds=dict(payload.get("thresholds") or {}),
            confidence_thresholds=dict(payload.get("confidence") or {}),
            score_min=int(payload.get("score_min", 0)),
            score_max=int(payload.get("score_max", 100)),
            version=int(payload.get("version", 1)),
            name=str(payload.get("name") or "default"),
            source=source,
        )


def default_scoring_rules() -> ScoringRules:
    """Load the packaged default rule set."""
    data = load_packaged_data("scoring_rules")
    return ScoringRules.from_dict(data, source="packaged:scoring_rules")


def load_scoring_rules(path: Optional[str | Path] = None) -> ScoringRules:
    """Load scoring rules from ``path``, falling back to packaged defaults.

    A user-supplied file is *merged* onto the defaults so you only need to
    specify the rules or thresholds you want to change — unless the file sets
    ``replace: true`` at the top level, in which case it is used as-is.
    """
    base = default_scoring_rules()
    if path is None:
        return base

    try:
        overlay = load_config_file(path)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        log.warning("Could not load scoring rules from %s (%s); using defaults", path, exc)
        return base

    if overlay.get("replace"):
        return ScoringRules.from_dict(overlay, source=str(path))

    base_dict = base.to_dict()
    overlay_rules = overlay.get("rules") or []
    if overlay_rules:
        by_id = {r.id: r for r in base.rules}
        for item in overlay_rules:
            rule = ScoringRule.from_dict(item)
            by_id[rule.id] = rule
        base_dict["rules"] = [by_id[r.id].to_dict() for r in base.rules]
        for item in overlay_rules:
            rule = ScoringRule.from_dict(item)
            if rule.id not in {r.id for r in base.rules}:
                base_dict["rules"].append(rule.to_dict())

    merged = merge_dicts(base_dict, {k: v for k, v in overlay.items() if k != "rules"})
    merged["rules"] = base_dict["rules"]
    return ScoringRules.from_dict(merged, source=str(path))


__all__ = [
    "ScoringRule",
    "ScoringRules",
    "load_scoring_rules",
    "default_scoring_rules",
]
