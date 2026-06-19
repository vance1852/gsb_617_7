from __future__ import annotations

from collections import deque
from typing import Iterable

from .models import TaskConfig


class CyclicDependencyError(Exception):
    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(f"Cyclic dependency detected: {' -> '.join(cycle)}")


class TaskGraph:
    def __init__(self, tasks: dict[str, TaskConfig]):
        self.tasks = tasks
        self._adj: dict[str, list[str]] = {n: [] for n in tasks}
        self._reverse_adj: dict[str, list[str]] = {n: [] for n in tasks}
        self._in_degree: dict[str, int] = {n: 0 for n in tasks}
        self._build()

    def _build(self) -> None:
        for name, task in self.tasks.items():
            for dep in task.depends_on:
                if dep not in self.tasks:
                    raise KeyError(f"Task '{name}' depends on unknown task '{dep}'")
                self._adj[dep].append(name)
                self._reverse_adj[name].append(dep)
                self._in_degree[name] += 1

    def detect_cycle(self) -> list[str] | None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in self.tasks}
        parent: dict[str, str | None] = {n: None for n in self.tasks}

        def dfs(u: str) -> list[str] | None:
            color[u] = GRAY
            for v in self._adj[u]:
                if color[v] == GRAY:
                    cycle = [v, u]
                    cur = u
                    while parent[cur] is not None and parent[cur] != v:
                        cur = parent[cur]
                        cycle.append(cur)
                    cycle.reverse()
                    return cycle
                if color[v] == WHITE:
                    parent[v] = u
                    result = dfs(v)
                    if result:
                        return result
            color[u] = BLACK
            return None

        for node in self.tasks:
            if color[node] == WHITE:
                cycle = dfs(node)
                if cycle:
                    return cycle
        return None

    def topological_sort(self) -> list[str]:
        cycle = self.detect_cycle()
        if cycle:
            raise CyclicDependencyError(cycle)

        in_deg = dict(self._in_degree)
        queue: deque[str] = deque([n for n, d in in_deg.items() if d == 0])
        order: list[str] = []

        while queue:
            u = queue.popleft()
            order.append(u)
            for v in self._adj[u]:
                in_deg[v] -= 1
                if in_deg[v] == 0:
                    queue.append(v)

        if len(order) != len(self.tasks):
            remaining = [n for n in self.tasks if n not in order]
            raise CyclicDependencyError(remaining)
        return order

    def get_dependencies(self, task_name: str) -> list[str]:
        return list(self._reverse_adj[task_name])

    def get_dependents(self, task_name: str) -> list[str]:
        return list(self._adj[task_name])

    def get_ready_tasks(self, completed: set[str], running: set[str]) -> list[str]:
        ready = []
        for name, task in self.tasks.items():
            if name in completed or name in running:
                continue
            deps_met = all(d in completed for d in task.depends_on)
            if deps_met:
                ready.append(name)
        return ready

    def resolve_selected_tasks(
        self,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
    ) -> set[str]:
        include_set = set(include) if include else None
        exclude_set = set(exclude) if exclude else set()

        if include_set is None:
            selected = set(self.tasks.keys())
        else:
            selected = set()
            stack = list(include_set)
            while stack:
                node = stack.pop()
                if node in selected or node in exclude_set:
                    continue
                if node not in self.tasks:
                    raise KeyError(f"Unknown task: {node}")
                selected.add(node)
                for dep in self._reverse_adj[node]:
                    if dep not in selected and dep not in exclude_set:
                        stack.append(dep)

        selected -= exclude_set
        return selected

    def get_execution_layers(self, selected: set[str] | None = None) -> list[list[str]]:
        if selected is None:
            selected = set(self.tasks.keys())

        in_deg: dict[str, int] = {}
        for name in selected:
            cnt = sum(1 for d in self.tasks[name].depends_on if d in selected)
            in_deg[name] = cnt

        layers: list[list[str]] = []
        remaining = set(selected)

        while remaining:
            layer = [n for n in remaining if in_deg[n] == 0]
            if not layer:
                raise CyclicDependencyError(list(remaining))
            layer.sort()
            layers.append(layer)
            for n in layer:
                remaining.remove(n)
                for m in self._adj[n]:
                    if m in in_deg:
                        in_deg[m] -= 1
        return layers
