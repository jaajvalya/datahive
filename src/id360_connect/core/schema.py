"""Schema fingerprinting and drift handling.

Fingerprint the schema at the START of every run and compare against the
registry. It costs one metadata call and it catches drift before you read a
single row - which is the difference between a clean quarantine and a
half-loaded table.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .errors import SchemaDrift
from .models import DriftPosture


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    nullable: bool = True


@dataclass
class Schema:
    fields: Sequence[Field] = field(default_factory=tuple)
    version: int = 1

    def fingerprint(self) -> str:
        """Order-independent: sorted `name:type:nullable` triples."""
        parts = sorted(f"{f.name}:{f.type}:{int(f.nullable)}" for f in self.fields)
        return "sha256:" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]

    def by_name(self) -> Mapping[str, Field]:
        return {f.name: f for f in self.fields}


@dataclass
class DriftReport:
    added: list[Field] = field(default_factory=list)
    removed: list[Field] = field(default_factory=list)
    type_changed: list[tuple[Field, Field]] = field(default_factory=list)
    nullability_relaxed: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.added or self.removed or self.type_changed
                    or self.nullability_relaxed)

    @property
    def breaking(self) -> bool:
        """Removals and narrowing type changes break downstream consumers."""
        return bool(self.removed) or any(
            not _is_widening(old.type, new.type) for old, new in self.type_changed)


#: Safe widening conversions. Anything not listed is treated as breaking.
_WIDENING = {
    ("int8", "int16"), ("int8", "int32"), ("int8", "int64"),
    ("int16", "int32"), ("int16", "int64"), ("int32", "int64"),
    ("float32", "float64"), ("int32", "float64"), ("int64", "decimal"),
    ("date", "timestamp"), ("string", "large_string"),
}


def _is_widening(old: str, new: str) -> bool:
    old, new = old.lower(), new.lower()
    if old == new:
        return True
    if old.startswith("decimal") and new.startswith("decimal"):
        return _decimal_widens(old, new)
    return (old, new) in _WIDENING


def _decimal_widens(old: str, new: str) -> bool:
    def parse(s: str) -> tuple[int, int]:
        inner = s[s.find("(") + 1:s.find(")")]
        p, _, sc = inner.partition(",")
        return int(p), int(sc or 0)
    try:
        (p1, s1), (p2, s2) = parse(old), parse(new)
    except (ValueError, IndexError):
        return False
    return p2 >= p1 and s2 >= s1


def diff(previous: Schema, current: Schema) -> DriftReport:
    prev, cur = previous.by_name(), current.by_name()
    report = DriftReport()
    for name, f in cur.items():
        if name not in prev:
            report.added.append(f)
        elif prev[name].type != f.type:
            report.type_changed.append((prev[name], f))
        elif prev[name].nullable is False and f.nullable is True:
            report.nullability_relaxed.append(name)
    for name, f in prev.items():
        if name not in cur:
            report.removed.append(f)
    return report


def apply_posture(report: DriftReport, posture: DriftPosture,
                  *, object_name: str = "") -> None:
    """Enforce the configured drift posture. Raises SchemaDrift to stop the run.

    strict     - any change fails the run
    evolve     - additive and widening changes are accepted; breaking changes
                 quarantine the batch and alert, but the PIPELINE KEEPS RUNNING
                 on the last-good schema (a breaking change upstream should not
                 take your platform down)
    permissive - everything is accepted; lossy casts are counted in metrics
    """
    if report.clean:
        return

    if posture is DriftPosture.STRICT:
        raise SchemaDrift(f"schema changed for {object_name} (posture=strict)",
                          added=[f.name for f in report.added],
                          removed=[f.name for f in report.removed],
                          changed=[o.name for o, _ in report.type_changed])

    if posture is DriftPosture.EVOLVE and report.breaking:
        raise SchemaDrift(
            f"breaking schema change for {object_name}; batch quarantined",
            added=[f.name for f in report.added],
            removed=[f.name for f in report.removed],
            changed=[o.name for o, _ in report.type_changed])

    # EVOLVE with only additive/widening changes, or PERMISSIVE: accept.
    return


def next_version(previous: Schema, report: DriftReport) -> int:
    return previous.version + (0 if report.clean else 1)
