"""Dependency graph: build, topological order, cycle detection."""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, Iterable, List, Set

from .config import TaskConfig


class GraphError(ValueError):
    pass


class DependencyGraph:
    def __init__(self, tasks: Iterable[TaskConfig]):
        self.tasks: Dict[str, TaskConfig] = {t.name: t for t in tasks}
        self.children: Dict[str, Set[str]] = defaultdict(set)
        self.parents: Dict[str, Set[str]] = defaultdict(set)
        self._build()

    def _build(self) -> None:
        for name, t in self.tasks.items():
            for dep in t.depends_on:
                if dep not in self.tasks:
                    raise GraphError(
                        f"Task '{name}' depends on unknown task '{dep}'."
                    )
                if dep == name:
                    raise GraphError(f"Task '{name}' cannot depend on itself.")
                self.parents[name].add(dep)
                self.children[dep].add(name)

    def detect_cycle(self) -> List[str]:
        """Return a list of task names forming a cycle, or [] if none."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {n: WHITE for n in self.tasks}
        parent: Dict[str, str] = {}
        cycle_path: List[str] = []

        def dfs(u: str) -> bool:
            color[u] = GRAY
            for v in sorted(self.children[u]):
                if color[v] == WHITE:
                    parent[v] = u
                    if dfs(v):
                        return True
                elif color[v] == GRAY:
                    cur = u
                    path = [v, u]
                    while cur in parent and parent[cur] != v:
                        cur = parent[cur]
                        path.append(cur)
                    path.reverse()
                    cycle_path.extend(path)
                    return True
            color[u] = BLACK
            return False

        for n in sorted(self.tasks):
            if color[n] == WHITE:
                if dfs(n):
                    return cycle_path
        return []

    def topological_order(self) -> List[str]:
        cycle = self.detect_cycle()
        if cycle:
            raise GraphError(f"Cyclic dependency detected: {' -> '.join(cycle)}")
        indeg: Dict[str, int] = {n: len(self.parents[n]) for n in self.tasks}
        queue: deque = deque(sorted(n for n, d in indeg.items() if d == 0))
        order: List[str] = []
        while queue:
            n = queue.popleft()
            order.append(n)
            for c in sorted(self.children[n]):
                indeg[c] -= 1
                if indeg[c] == 0:
                    queue.append(c)
        if len(order) != len(self.tasks):
            raise GraphError("Cyclic dependency detected during topological sort.")
        return order

    def filter(self, include: List[str], exclude: List[str]) -> Set[str]:
        """Return the set of task names to run, expanding includes via deps."""
        all_names = set(self.tasks)
        for n in include + exclude:
            if n not in all_names:
                raise GraphError(f"Unknown task in include/exclude: {n}")

        if include:
            selected: Set[str] = set()
            stack = list(include)
            while stack:
                n = stack.pop()
                if n in selected:
                    continue
                selected.add(n)
                stack.extend(self.parents[n])
        else:
            selected = set(all_names)

        if exclude:
            ex_set: Set[str] = set()
            stack = list(exclude)
            while stack:
                n = stack.pop()
                if n in ex_set:
                    continue
                ex_set.add(n)
                stack.extend(self.children[n])
            selected -= ex_set

        return selected
