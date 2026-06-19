from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, Iterable, List, Set, Tuple


class CyclicDependencyError(Exception):
    def __init__(self, cycle: List[str]):
        self.cycle = cycle
        super().__init__(f"Cyclic dependency detected: {' -> '.join(cycle)}")


class TaskGraph:
    def __init__(self) -> None:
        self._nodes: Set[str] = set()
        self._forward: Dict[str, Set[str]] = defaultdict(set)
        self._reverse: Dict[str, Set[str]] = defaultdict(set)

    def add_task(self, name: str, depends_on: Iterable[str] = ()) -> None:
        self._nodes.add(name)
        for dep in depends_on:
            self._nodes.add(dep)
            self._forward[dep].add(name)
            self._reverse[name].add(dep)

    def dependencies(self, name: str) -> Set[str]:
        return frozenset(self._reverse.get(name, set()))

    def dependents(self, name: str) -> Set[str]:
        return frozenset(self._forward.get(name, set()))

    def all_tasks(self) -> Set[str]:
        return frozenset(self._nodes)

    def detect_cycle(self) -> List[str] | None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {n: WHITE for n in self._nodes}
        parent: Dict[str, str | None] = {}

        def dfs(start: str) -> List[str] | None:
            stack: List[Tuple[str, Iterable[str]]] = [(start, iter(self._reverse.get(start, set())))]
            color[start] = GRAY
            parent[start] = None
            while stack:
                node, it = stack[-1]
                try:
                    nxt = next(it)
                except StopIteration:
                    color[node] = BLACK
                    stack.pop()
                    continue
                if color[nxt] == GRAY:
                    cycle = [nxt, node]
                    cur = node
                    while parent.get(cur) is not None and parent[cur] != nxt:
                        cur = parent[cur]
                        cycle.append(cur)
                    cycle.append(nxt)
                    cycle.reverse()
                    return cycle
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    parent[nxt] = node
                    stack.append((nxt, iter(self._reverse.get(nxt, set()))))
            return None

        for node in self._nodes:
            if color[node] == WHITE:
                cycle = dfs(node)
                if cycle:
                    return cycle
        return None

    def topological_levels(self) -> List[List[str]]:
        cycle = self.detect_cycle()
        if cycle:
            raise CyclicDependencyError(cycle)

        in_degree: Dict[str, int] = {n: len(self._reverse.get(n, set())) for n in self._nodes}
        current: deque[str] = deque(n for n in self._nodes if in_degree[n] == 0)
        levels: List[List[str]] = []
        visited = 0

        while current:
            level = []
            for _ in range(len(current)):
                node = current.popleft()
                level.append(node)
                visited += 1
                for dep in self._forward.get(node, set()):
                    in_degree[dep] -= 1
                    if in_degree[dep] == 0:
                        current.append(dep)
            levels.append(level)

        if visited != len(self._nodes):
            remaining = [n for n in self._nodes if in_degree[n] > 0]
            sub_cycle = self.detect_cycle()
            if sub_cycle:
                raise CyclicDependencyError(sub_cycle)
            raise CyclicDependencyError(remaining)

        return levels

    def transitive_deps(self, name: str) -> Set[str]:
        result: Set[str] = set()
        stack = list(self._reverse.get(name, set()))
        while stack:
            d = stack.pop()
            if d not in result:
                result.add(d)
                stack.extend(self._reverse.get(d, set()))
        return result

    def transitive_dependents(self, name: str) -> Set[str]:
        result: Set[str] = set()
        stack = list(self._forward.get(name, set()))
        while stack:
            d = stack.pop()
            if d not in result:
                result.add(d)
                stack.extend(self._forward.get(d, set()))
        return result

    def filtered_subgraph(
        self,
        include: Set[str] | None = None,
        exclude: Set[str] | None = None,
    ) -> "TaskGraph":
        selected: Set[str] = set(self._nodes)
        if include is not None:
            closure: Set[str] = set()
            stack = list(include)
            while stack:
                n = stack.pop()
                if n not in closure and n in self._nodes:
                    closure.add(n)
                    stack.extend(self._reverse.get(n, set()))
            selected &= closure
        if exclude is not None:
            excl_closure: Set[str] = set()
            stack = list(exclude)
            while stack:
                n = stack.pop()
                if n not in excl_closure and n in self._nodes:
                    excl_closure.add(n)
                    stack.extend(self._forward.get(n, set()))
            selected -= excl_closure

        g = TaskGraph()
        for n in selected:
            deps = self._reverse.get(n, set()) & selected
            g.add_task(n, deps)
        return g


def build_graph(tasks: Dict[str, "TaskConfig"]) -> TaskGraph:
    from .config import TaskConfig
    g = TaskGraph()
    for name, tc in tasks.items():
        g.add_task(name, tc.depends_on)
    return g
