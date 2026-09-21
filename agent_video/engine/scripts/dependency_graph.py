"""Shared dependency-graph resolution for source-backed speech atoms.

Every caller uses the same fail-closed semantics: dependencies are recursive,
missing atoms invalidate their dependants, and cycles invalidate the entire
cycle plus every ancestor that reaches it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class DependencyResolution:
    valid_ids: frozenset[int]
    invalid_ids: frozenset[int]
    missing_atoms: frozenset[str]
    cycles: tuple[tuple[int, ...], ...]


def resolve_dependency_closure(
        rows: list[dict[str, Any]], requested_ids: Iterable[int] | None = None,
        *, id_key: str = "i") -> DependencyResolution:
    """Return the recursive, valid closure for ``requested_ids``.

    Rows outside the requested closure are ignored.  A row is valid only when
    all descendants exist and are acyclic.  This naturally propagates a deep
    missing dependency or cycle back through every dependent ancestor.
    """
    by_id = {int(row.get(id_key, -1)): row for row in rows
             if row.get(id_key) is not None and int(row.get(id_key, -1)) >= 0}
    by_atom = {str(row.get("atom_id")): row for row in rows if row.get("atom_id")}
    roots = set(by_id) if requested_ids is None else {int(value) for value in requested_ids}
    state: dict[int, int] = {}
    memo: dict[int, bool] = {}
    stack: list[int] = []
    invalid: set[int] = set()
    missing: set[str] = set()
    cycles: list[tuple[int, ...]] = []

    def visit(candidate_id: int) -> bool:
        if candidate_id not in by_id:
            invalid.add(candidate_id)
            return False
        if candidate_id in memo:
            return memo[candidate_id]
        if state.get(candidate_id) == 1:
            start = stack.index(candidate_id)
            cycle = tuple(stack[start:] + [candidate_id])
            if cycle not in cycles:
                cycles.append(cycle)
            invalid.update(cycle)
            return False
        state[candidate_id] = 1
        stack.append(candidate_id)
        ok = True
        for atom in by_id[candidate_id].get("required_atom_ids") or []:
            atom = str(atom)
            dependency = by_atom.get(atom)
            if dependency is None:
                missing.add(atom)
                ok = False
                continue
            dependency_id = int(dependency.get(id_key, -1))
            if not visit(dependency_id):
                ok = False
        stack.pop()
        state[candidate_id] = 2
        memo[candidate_id] = ok
        if not ok:
            invalid.add(candidate_id)
        return ok

    valid_roots = {candidate_id for candidate_id in roots if visit(candidate_id)}
    valid: set[int] = set()

    def collect(candidate_id: int) -> None:
        if candidate_id in valid:
            return
        valid.add(candidate_id)
        for atom in by_id[candidate_id].get("required_atom_ids") or []:
            dependency = by_atom.get(str(atom))
            if dependency is not None:
                dependency_id = int(dependency.get(id_key, -1))
                if memo.get(dependency_id, False):
                    collect(dependency_id)

    for candidate_id in valid_roots:
        collect(candidate_id)
    return DependencyResolution(frozenset(valid), frozenset(invalid),
                                frozenset(missing), tuple(cycles))


def dependency_issues(rows: list[dict[str, Any]], *, id_key: str = "_candidate_id"
                      ) -> list[dict[str, Any]]:
    """Describe graph failures in a selected timeline for final hard gates."""
    if not any(row.get("required_atom_ids") for row in rows):
        return []
    normalized = []
    for index, row in enumerate(rows):
        item = dict(row)
        value = row.get(id_key)
        item[id_key] = index if value is None else int(value)
        normalized.append(item)
    result = resolve_dependency_closure(
        normalized, (int(item[id_key]) for item in normalized), id_key=id_key)
    issues = []
    if result.missing_atoms:
        issues.append({"code": "missing_dependency", "level": "error",
                       "detail": "缺少依赖 atom：" + ", ".join(sorted(result.missing_atoms))})
    if result.cycles:
        issues.append({"code": "dependency_cycle", "level": "error",
                       "detail": f"依赖图包含 {len(result.cycles)} 个环",
                       "cycles": [list(cycle) for cycle in result.cycles]})
    return issues


def filter_dependency_valid_rows(rows: list[dict[str, Any]]
                                 ) -> tuple[list[dict[str, Any]], DependencyResolution]:
    """Drop every selected row whose dependency subtree is invalid."""
    graph_rows = [dict(row, _dependency_index=index) for index, row in enumerate(rows)]
    result = resolve_dependency_closure(
        graph_rows, range(len(graph_rows)), id_key="_dependency_index")
    return ([row for index, row in enumerate(rows) if index in result.valid_ids], result)
