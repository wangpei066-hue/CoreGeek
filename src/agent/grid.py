"""网格路径规划与碰撞检测算法。"""
import heapq
from typing import Optional, Generator

from .protocol import Pos, MatchState, Role


_NEIGHBOR_OFFSETS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def chebyshev(a: Pos, b: Pos) -> int:
    """切比雪夫距离（棋盘距离）"""
    return max(abs(a.x - b.x), abs(a.y - b.y))


def neighbors8(pos: Pos, width: int, height: int) -> Generator[Pos, None, None]:
    """8方向邻接单元迭代"""
    for dx, dy in _NEIGHBOR_OFFSETS:
        nx, ny = pos.x + dx, pos.y + dy
        if 0 <= nx < width and 0 <= ny < height:
            yield Pos(nx, ny)


def astar_next_step(start: Pos, goal: Pos, blocked: set, width: int, height: int) -> Optional[Pos]:
    """8方向A*寻路（代价1，启发式为切比雪夫距离），返回从start走向goal的下一步单格坐标。"""
    if (start.x, start.y) == (goal.x, goal.y):
        return None
    goal_key = (goal.x, goal.y)
    open_heap = [(chebyshev(start, goal), 0, (start.x, start.y))]
    came_from = {(start.x, start.y): None}
    g_score = {(start.x, start.y): 0}
    visited = set()
    while open_heap:
        _, g, current = heapq.heappop(open_heap)
        if current in visited:
            continue
        visited.add(current)
        if current == goal_key:
            node = current
            path = []
            while came_from[node] is not None:
                path.append(node)
                node = came_from[node]
            path.reverse()
            return Pos(*path[0]) if path else None
        for n in neighbors8(Pos(*current), width, height):
            key = (n.x, n.y)
            if key != goal_key and key in blocked:
                continue
            tentative = g + 1
            if tentative < g_score.get(key, 1_000_000_000):
                g_score[key] = tentative
                came_from[key] = current
                heapq.heappush(open_heap, (tentative + chebyshev(n, goal), tentative, key))
    return None


def nearest_adjacent_free_cell(start: Pos, target: Pos, blocked: set, width: int, height: int) -> Optional[Pos]:
    """在target周围寻找离start最近的空闲相邻单元"""
    candidates = [n for n in neighbors8(target, width, height) if (n.x, n.y) not in blocked]
    if not candidates:
        return None
    return min(candidates, key=lambda c: chebyshev(start, c))


def move_towards(start: Pos, target: Pos, blocked: set, width: int, height: int) -> Optional[Pos]:
    """返回本回合应移动到的下一格；已在target一格范围内则返回None（可直接行动，无需移动）。"""
    if chebyshev(start, target) <= 1:
        return None
    goal = nearest_adjacent_free_cell(start, target, blocked, width, height)
    if goal is None:
        return None
    return astar_next_step(start, goal, blocked, width, height)


def build_blocked_set(state: "MatchState") -> set:
    """构建阻挡集合：己方/敌方建筑与角色、机器人（任务书4.1）。中立元素（矿区/小贩/商店/任务点）
    与队伍角色分别处理；基地为2x2，pos是左上角坐标（接口文档1.3.1注）。"""
    blocked = set()
    if state.map_info:
        for zone in state.map_info.zones:
            blocked.add((zone.pos.x, zone.pos.y))

    def add_role(role: Role):
        if role.role_type == "station":
            bx, by = role.pos.x, role.pos.y
            for dx in (0, 1):
                for dy in (0, 1):
                    blocked.add((bx + dx, by + dy))
        else:
            blocked.add((role.pos.x, role.pos.y))

    if state.team_our:
        for role in state.team_our.roles:
            add_role(role)
    if state.team_enemy:
        for role in state.team_enemy.roles:
            add_role(role)
    if state.robot:
        for robot in state.robot.roles:
            blocked.add((robot.pos.x, robot.pos.y))
    return blocked
