"""夜间武器选目标：按有效伤害（不计溢出）× 每点伤害价值 + 击杀奖励贪心选落点。

数值来自任务书4.5.1/4.5.4/4.7.2；电磁炮/加特林弹道的直线判定是本地近似，
官方未给出完整遮挡算法（见 docs/rules_verified.md）。
一回合内所有武器共用一份 DamageLedger：先算火箭，再算电磁炮补刀，避免多门炮重复打死同一只。
第五天起 BOSS 刷新后，火箭先在能打到 BOSS 的落点里选（中心或溅射），再比收益；
射程够不着才退回打密集小怪。BOSS 远距离威胁系数不低于 BOSS_MIN_URGENCY。
"""
from math import hypot
from typing import Optional

from .grid import chebyshev
from .protocol import Pos

# roleType: (满血, 积分, 攻击力)
ROBOT_STATS = {
    "smallRobot": (40, 1, 5),
    "middleRobot": (60, 2, 10),
    "largeRobot": (500, 4, 20),
    "bossRobot": (800, 10, 40),
}
ROCKET_CENTER = 20
ROCKET_SPLASH = 10
GATLING_BULLET = 10
RAILGUN_ENERGY_PER_LEVEL = 10
KILL_BONUS = 0.5          # 打死时额外加 score × KILL_BONUS
ATTACKING_RANGE = 3       # 机器人射程3：距我方建筑 ≤3 视为正在攻击
ATTACKING_URGENCY = 3.0
APPROACH_SPAN = 10        # 距离 3→13 时威胁系数从 1 线性降到 0
BOSS_PRIORITY_DAY = 5     # 第五天起 BOSS 刷新：火箭优先打能溅到 BOSS 的落点
BOSS_MIN_URGENCY = 1.0    # BOSS 会走到墙下，刷新边威胁不能按 0 算
LATE_NIGHT_ROUNDS = 10    # 最后这么多回合不再投资天亮前打不死的目标
NIGHT_ROUNDS = 60
CYCLE_ROUNDS = 130
_BUILDINGS = ("station", "wall", "gatling", "railgun", "rocket")


class DamageLedger:
    """本回合已分配、回合末才结算的预计伤害。"""

    def __init__(self):
        self.pending = {}

    def remaining(self, robot) -> int:
        return robot.health - self.pending.get(robot.id, 0)

    def add(self, damage: dict):
        for rid, dmg in damage.items():
            self.pending[rid] = self.pending.get(rid, 0) + dmg


class TargetContext:
    """每回合算一次的机器人价值表。"""

    def __init__(self, state, robots):
        self.robots = [r for r in robots if r.health > 0]
        self.by_id = {r.id: r for r in self.robots}
        self.by_pos = {}
        for r in self.robots:
            self.by_pos.setdefault((r.pos.x, r.pos.y), []).append(r)
        buildings = [b for b in (state.team_our.roles if state and state.team_our else [])
                     if b.role_type in _BUILDINGS and b.health > 0]
        rounds_left = _night_rounds_left(state)
        capacity = _kill_capacity(state, rounds_left)
        from .news_memory import game_day
        self.prioritize_boss = game_day(getattr(state, "round_no", None)) >= BOSS_PRIORITY_DAY
        self.value = {}
        for r in self.robots:
            hp_max, score, atk = ROBOT_STATS.get(r.role_type, (max(r.health, 1), 0, 0))
            dist = _distance_to_buildings(r.pos, buildings)
            if dist <= ATTACKING_RANGE:
                urgency = ATTACKING_URGENCY
            else:
                urgency = max(0.0, 1.0 - (dist - ATTACKING_RANGE) / APPROACH_SPAN)
            if r.role_type == "bossRobot":
                # 800 血摊到积分上极低；刷新边距建筑 >13 时威胁原公式是 0，
                # 火箭会去打近处小怪，BOSS 一路走到墙根都挨不到。
                urgency = max(urgency, BOSS_MIN_URGENCY)
            if rounds_left <= LATE_NIGHT_ROUNDS and r.health > capacity and urgency < ATTACKING_URGENCY:
                self.value[r.id] = (0.0, 0.0)  # 天亮会被清除，打不死的伤害白费
                continue
            self.value[r.id] = ((score + atk * urgency) / hp_max, score * KILL_BONUS)

    def gain(self, damage: dict, ledger: DamageLedger) -> float:
        total = 0.0
        for rid, dmg in damage.items():
            r = self.by_id.get(rid)
            if r is None or not dmg:
                continue
            left = ledger.remaining(r)
            if left <= 0:
                continue
            per_hp, kill = self.value[r.id]
            total += per_hp * min(dmg, left)
            if dmg >= left:
                total += kill
        return total


def _night_rounds_left(state) -> int:
    round_no = getattr(state, "round_no", None) or 0
    cycle = round_no % CYCLE_ROUNDS
    night_start = CYCLE_ROUNDS - NIGHT_ROUNDS
    if cycle < night_start:
        return NIGHT_ROUNDS
    return CYCLE_ROUNDS - cycle


def _kill_capacity(state, rounds_left) -> int:
    """剩余回合内对单个目标的最大可打伤害（火箭中心叠加 + 电磁炮）。"""
    total = 0
    for w in (state.team_our.roles if state and state.team_our else []):
        if w.health <= 0:
            continue
        level = w.level or 1
        if w.role_type == "rocket":
            total += (rounds_left // 4 + 1) * ROCKET_CENTER * level
        elif w.role_type == "railgun":
            total += rounds_left * RAILGUN_ENERGY_PER_LEVEL * level
        elif w.role_type == "gatling":
            total += rounds_left * GATLING_BULLET * level
    return total


def _distance_to_buildings(pos: Pos, buildings) -> int:
    best = 99
    for b in buildings:
        d = chebyshev(pos, b.pos)
        if b.role_type == "station":
            d = max(0, d - 1)  # 基地 2×2
        best = min(best, d)
    return best


def _level(weapon) -> int:
    return weapon.level or 1


def _in_range(weapon, x, y) -> bool:
    return max(abs(weapon.pos.x - x), abs(weapon.pos.y - y)) <= (weapon.attack_range or 0)


def _rocket_damage(ctx: TargetContext, x: int, y: int) -> dict:
    damage = {}
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            dmg = ROCKET_CENTER if dx == 0 and dy == 0 else ROCKET_SPLASH
            for r in ctx.by_pos.get((x + dx, y + dy), ()):
                damage[r.id] = damage.get(r.id, 0) + dmg
    return damage


def _boss_lock_cells(ctx: TargetContext, cells, ledger: DamageLedger):
    """第五天起：只保留能打到仍存活 BOSS 的落点（中心或溅射）。射程够不着则不锁。"""
    if not getattr(ctx, "prioritize_boss", False):
        return None
    bosses = [r for r in ctx.robots
              if r.role_type == "bossRobot" and ledger.remaining(r) > 0]
    if not bosses:
        return None
    ids = {r.id for r in bosses}
    locked = {cell for cell in cells if ids.intersection(_rocket_damage(ctx, *cell))}
    return locked or None


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0) + v
    return out


def plan_rocket(weapon, ctx: TargetContext, ledger: DamageLedger):
    """逐枚导弹贪心；返回 (targetPos 列表, 伤害表) 或 None。"""
    cells = set()
    for r in ctx.robots:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                x, y = r.pos.x + dx, r.pos.y + dy
                if x >= 0 and y >= 0 and _in_range(weapon, x, y):
                    cells.add((x, y))
    if not cells:
        return None
    search = _boss_lock_cells(ctx, cells, ledger) or cells
    scratch = DamageLedger()
    scratch.pending = dict(ledger.pending)
    positions, damage = [], {}
    for _ in range(_level(weapon)):
        best, best_cell, best_dmg = 0.0, None, None
        for cell in sorted(search):
            dmg = _rocket_damage(ctx, *cell)
            g = ctx.gain(dmg, scratch)
            if g > best:
                best, best_cell, best_dmg = g, cell, dmg
        if best_cell is None:
            if not positions:
                return None
            best_cell = (positions[0]["x"], positions[0]["y"])  # 目标数须等于等级，重复首发落点
            best_dmg = _rocket_damage(ctx, *best_cell)
        positions.append({"x": best_cell[0], "y": best_cell[1]})
        scratch.add(best_dmg)
        damage = _merge(damage, best_dmg)
    # 逐枚贪心看不到"几枚叠加才打死"的收益，再比较整轮叠在同一格的方案。
    level = _level(weapon)
    best_total = ctx.gain(damage, ledger)
    for cell in sorted(search):
        stacked = {rid: dmg * level for rid, dmg in _rocket_damage(ctx, *cell).items()}
        g = ctx.gain(stacked, ledger)
        if g > best_total + 1e-9:
            best_total, damage = g, stacked
            positions = [{"x": cell[0], "y": cell[1]}] * level
    return positions, damage


def _robots_on_segment(ctx: TargetContext, start: Pos, end_x: int, end_y: int):
    """起点到终点中心连线经过的机器人，按距离排序；格心到线段距离 <0.5 视为在弹道上（本地近似）。"""
    sx, sy = start.x, start.y
    vx, vy = end_x - sx, end_y - sy
    length2 = vx * vx + vy * vy
    hits = []
    for r in ctx.robots:
        px, py = r.pos.x - sx, r.pos.y - sy
        if length2 == 0:
            continue
        t = (px * vx + py * vy) / length2
        if t <= 0 or t > 1:
            continue
        if hypot(px - t * vx, py - t * vy) < 0.5:
            hits.append((t, r))
    hits.sort(key=lambda h: (h[0], h[1].id))
    return [r for _, r in hits]


def plan_railgun(weapon, ctx: TargetContext, ledger: DamageLedger):
    """电磁能量沿弹道按实际伤害扣减，到达落点即止；返回 ([落点], 伤害表) 或 None。"""
    energy_max = RAILGUN_ENERGY_PER_LEVEL * _level(weapon)
    best = (0.0, None, None)
    for r in sorted(ctx.robots, key=lambda r: r.id):
        if not _in_range(weapon, r.pos.x, r.pos.y):
            continue
        energy, damage = energy_max, {}
        for hit in _robots_on_segment(ctx, weapon.pos, r.pos.x, r.pos.y):
            left = ledger.remaining(hit)
            if left <= 0:
                continue
            dmg = min(energy, left)
            damage[hit.id] = dmg
            energy -= dmg
            if energy <= 0:
                break
        g = ctx.gain(damage, ledger)
        if g > best[0]:
            best = (g, r, damage)
    if best[1] is None:
        return None
    return [{"x": best[1].pos.x, "y": best[1].pos.y}], best[2]


def _same_cone(weapon, points) -> bool:
    """任意两个目标相对加特林方向夹角 ≤90°（点积 ≥0）。"""
    vecs = [(x - weapon.pos.x, y - weapon.pos.y) for x, y in points]
    return all(a[0] * b[0] + a[1] * b[1] >= 0 for i, a in enumerate(vecs) for b in vecs[i + 1:])


def plan_gatling(weapon, ctx: TargetContext, ledger: DamageLedger):
    """每颗子弹只打弹道上最近的机器人，只选自己就是第一只的目标；多颗须在同一90°锥形内。"""
    visible = []
    for r in sorted(ctx.robots, key=lambda r: r.id):
        if not _in_range(weapon, r.pos.x, r.pos.y):
            continue
        path = _robots_on_segment(ctx, weapon.pos, r.pos.x, r.pos.y)
        if path and path[0].id == r.id:
            visible.append(r)
    if not visible:
        return None
    scratch = DamageLedger()
    scratch.pending = dict(ledger.pending)
    chosen, damage = [], {}
    for _ in range(_level(weapon)):
        best, pick = 0.0, None
        for r in visible:
            if not _same_cone(weapon, [(c.pos.x, c.pos.y) for c in chosen] + [(r.pos.x, r.pos.y)]):
                continue
            g = ctx.gain({r.id: GATLING_BULLET}, scratch)
            if g > best:
                best, pick = g, r
        if pick is None:
            if not chosen:
                return None
            pick = chosen[0]
        chosen.append(pick)
        scratch.add({pick.id: GATLING_BULLET})
        damage = _merge(damage, {pick.id: GATLING_BULLET})
    return [{"x": r.pos.x, "y": r.pos.y} for r in chosen], damage


_PLANNERS = {"rocket": plan_rocket, "railgun": plan_railgun, "gatling": plan_gatling}


def plan_attack(weapon, robots, state=None, ledger: Optional[DamageLedger] = None,
                ctx: Optional[TargetContext] = None):
    """返回 (targetPos 列表, 伤害表)；射程内没有值得打的目标返回 None。不修改 ledger。"""
    planner = _PLANNERS.get(weapon.role_type)
    if planner is None:
        return None
    ctx = ctx or TargetContext(state, robots)
    return planner(weapon, ctx, ledger or DamageLedger())
