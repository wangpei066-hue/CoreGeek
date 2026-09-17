"""开拓者接任务调度：预约、耗时估计、stderr 记录。不改求解算法。"""
from .decision_log import trace
from .grid import chebyshev, move_towards
from .log_format import emit_stderr
from .protocol import Pos
from .task_solver import MIN_TASK_TIMEOUT_ROUNDS, MARKER, task_context, task_fingerprint


SCHEDULER_VERSION = '20260915-sched2'
# 领取到提交的观测回退：Alpha/Beta 部署约 9 轮（探查+修复+验收+提交），不是优化目标。
DEPLOY_SOLVE_ROUNDS = 9
DEPLOY_SOLVE_NOTE = '观测部署领取到提交约9轮：probe/fix/check/submit，含失败余量'
API_SOLVE_ROUNDS = 9
API_SOLVE_NOTE = 'API尚无稳定实测，保守回退9轮，与部署分开配置'
UNKNOWN_SOLVE_ROUNDS = 9
UNKNOWN_SOLVE_NOTE = '领取前不知题型，取各类回退与经验的保守上界'
ESTIMATED_SOLVE_ROUNDS = DEPLOY_SOLVE_ROUNDS
ACCEPT_RANGE = 1
SHOP_STALL_ROUNDS = 4
RESERVATION_KEY = 'pioneer_task_reservation'
SHOP_PROGRESS_KEY = 'pioneer_shop_progress'
TASK_STREAK_KEY = 'pioneer_task_streaks'
INTERRUPT_RESERVATION_COST = 24
TASK_TYPES = ('自进化类1', '自进化类2')
KIND_FALLBACKS = {
    'workspace': (DEPLOY_SOLVE_ROUNDS, 'config.DEPLOY_SOLVE_ROUNDS ' + DEPLOY_SOLVE_NOTE),
    'api': (API_SOLVE_ROUNDS, 'config.API_SOLVE_ROUNDS ' + API_SOLVE_NOTE),
    'unknown': (UNKNOWN_SOLVE_ROUNDS, 'config.UNKNOWN_SOLVE_ROUNDS ' + UNKNOWN_SOLVE_NOTE),
}


def scheduler_task_session(state):
    """decide 读到的 session 可能是上一回合求解结果；过期会话不能影响接取/回防。"""
    session = getattr(state, 'task_session', None) or {}
    if not isinstance(session, dict) or not session:
        return {}
    if not state.phase_task:
        return {}
    fingerprint = session.get('fingerprint')
    if fingerprint and fingerprint != task_fingerprint(state.phase_task):
        return {}
    key = session.get('key')
    if key and state.team_our and len(key) >= 3 and key[2] and key[2] != state.phase_task:
        return {}
    return session


def _duration_from_experience(experience, kind, fallback):
    samples = ((experience or {}).get('durations') or {}).get(kind) or []
    values = [int(item['duration']) for item in samples if item.get('duration')]
    if not values:
        return fallback, KIND_FALLBACKS[kind][1]
    values.sort()
    idx = min(len(values) - 1, max(0, (len(values) * 3) // 4))
    estimate = max(fallback, values[idx])
    return estimate, 'experience.p75_including_failures kind=%s n=%d fallback=%d' % (
        kind, len(values), fallback)


def estimated_solve_rounds(state):
    session = scheduler_task_session(state)
    if session.get('metrics', {}).get('answerReadyRound') or session.get('answer'):
        return 1, 'session.answer_ready'
    experience = getattr(state, 'task_experience', None) or {}
    kind = None
    if session.get('taskKind'):
        kind = session.get('taskKind')
    elif state.phase_task:
        kind = task_context(state.phase_task).get('taskKind')
    if kind in KIND_FALLBACKS:
        fallback, _source = KIND_FALLBACKS[kind]
        return _duration_from_experience(experience, kind, fallback)
    estimates = []
    for item_kind, (fallback, _source) in KIND_FALLBACKS.items():
        estimate, source = _duration_from_experience(experience, item_kind, fallback)
        estimates.append((estimate, source, item_kind))
    estimate, source, item_kind = max(estimates, key=lambda row: row[0])
    return estimate, 'pre_accept.max(%s) %s' % (item_kind, source)


def reservation_of(state):
    memory = state.policy_memory or {}
    item = memory.get(RESERVATION_KEY)
    return item if isinstance(item, dict) else None


def has_task_reservation(state, role=None):
    """进行中的 phaseTask、前往中或领取待确认才算占用。phaseTask 已清空后的 active 是过期占用。"""
    if state.phase_task:
        return True
    item = reservation_of(state)
    if not item:
        return False
    if role is not None and item.get('pioneerId') not in (None, role.id):
        return False
    return item.get('stage') in (None, 'approaching', 'accept_pending')


def clear_reservation(state, reason):
    item = reservation_of(state)
    if not item:
        return
    trace(state, item.get('pioneerId'), 'task_reservation_cleared', '清理任务预约',
          reason=reason, reservation=item)
    state.policy_memory.pop(RESERVATION_KEY, None)


def matching_task(state, reservation):
    if not reservation or not state.team_our:
        return None
    for task in state.team_our.player_tasks:
        if (task.task_type == reservation.get('taskType')
                and task.task_position.x == reservation.get('x')
                and task.task_position.y == reservation.get('y')):
            return task
    return None


def row_matches_reservation(row, reservation):
    return bool(reservation) and row.get('taskType') == reservation.get('taskType') and (
        row.get('x') == reservation.get('x') and row.get('y') == reservation.get('y'))


def forced_defense(state):
    from .tactics import imminent_contact, pressure
    if pressure(state):
        return True, 'pressure'
    if imminent_contact(state):
        return True, 'imminent_contact'
    return False, None


def voucher_is_defense_critical(state):
    from .tactics import front_breached
    forced, reason = forced_defense(state)
    if forced:
        return True, reason
    if front_breached(state):
        return True, 'front_breached'
    return False, None


def defense_snapshot(role, state, blocked):
    from .opening import MUSTER_BUFFER, station_return_detail
    from .tactics import imminent_contact, night_wave_cleared, pressure, threat_eta_to_base
    from .brain import is_day_round
    detail = station_return_detail(role, state, blocked)
    travel = detail['steps']
    eta = threat_eta_to_base(state, role)
    wave = night_wave_cleared(state)
    press = pressure(state)
    contact = imminent_contact(state)
    day = is_day_round(state.round_no)
    from .opening import defense_rounds_remaining
    slack = defense_rounds_remaining(state, role)
    reasons = []
    due = False
    if press:
        reasons.append('pressure')
        due = True
    if contact:
        reasons.append('imminent_contact')
        due = True
    if travel is None:
        reasons.append(detail.get('reason') or 'no_return_path')
        due = True
    elif due:
        pass
    elif wave:
        due = False
    elif not day and slack <= 0:
        reasons.append('night_not_cleared')
        due = True
    elif eta is None:
        reasons.append('no_threat_eta')
        due = True
    elif travel + MUSTER_BUFFER >= eta:
        reasons.append('travel_plus_buffer_vs_eta')
        due = True
    return dict(
        pressure=press, imminentContact=contact, travel=travel,
        travelSource='station_return_detail/same as defense_due',
        travelReason=detail.get('reason'), alreadyAtPost=detail.get('alreadyAtPost'),
        weaponId=detail.get('weaponId'), stand=detail.get('stand'),
        threatEta=eta,
        threatEtaSource='threat_eta_to_base=min(夜防到位剩余,可见敌人切比雪夫下界)',
        musterBuffer=MUSTER_BUFFER, nightWaveCleared=wave,
        defenseDue=due, defenseDueReasons=reasons, defenseSlack=slack,
    )


def evaluate_task_candidates(pioneer, state, blocked, reserved=None):
    from .opening import MUSTER_BUFFER, adjacent_path, station_return_detail
    from .brain import is_day_round
    from .tactics import night_wave_cleared, threat_eta_to_base
    solve, solve_source = estimated_solve_rounds(state)
    eta = threat_eta_to_base(state, pioneer)
    # 夜里只有机器人清完（试探清波）后才允许接新任务；未清波时开拓者固定守炮。
    wave = night_wave_cleared(state)
    obstacles = blocked if reserved is None else (blocked | reserved)
    rows = []
    tasks = []
    if state.team_our:
        tasks = [t for t in state.team_our.player_tasks if t.task_type in TASK_TYPES]
    for task in tasks:
        in_range = chebyshev(pioneer.pos, task.task_position) <= ACCEPT_RANGE
        row = dict(
            taskType=task.task_type, x=task.task_position.x, y=task.task_position.y,
            isValid=task.is_valid, coldDownRounds=task.cold_down_rounds,
            timeoutRounds=task.timeout_rounds, rejected=None,
            outbound=None, outboundSource='adjacent_path(领取邻格)',
            solveEstimate=solve, solveSource=solve_source,
            returnSteps=None, returnSource='station_return_detail(from accept stand)',
            returnReason=None, alreadyAtPost=None, acceptStand=None,
            available=eta, availableSource='threat_eta_to_base',
            needed=None, neededSource=None, inAcceptRange=in_range,
            taskConstraint=None, defenseConstraint=None,
        )
        if not task.is_valid:
            row['rejected'] = 'invalid'
            rows.append(row)
            continue
        if task.cold_down_rounds:
            row['rejected'] = 'cooldown'
            rows.append(row)
            continue
        from .opening import defense_rounds_remaining
        if not is_day_round(state.round_no) and not wave and defense_rounds_remaining(state, pioneer) <= 0:
            row['rejected'] = 'night_defense'
            rows.append(row)
            continue
        timeout = task.timeout_rounds
        row['taskConstraint'] = dict(
            timeoutRounds=timeout, solveEstimate=solve,
            ok=timeout is None or solve <= timeout,
            note='领取后预计完成耗时须≤平台时限；timeoutRounds≥4不代表可完成',
        )
        if timeout is not None and timeout < MIN_TASK_TIMEOUT_ROUNDS:
            row['rejected'] = 'platform_timeout_too_short'
            rows.append(row)
            continue
        if timeout is not None and solve > timeout:
            row['rejected'] = 'solve_exceeds_platform_timeout'
            rows.append(row)
            continue
        if is_day_round(state.round_no):
            route = adjacent_path(pioneer, task.task_position, obstacles, state)
        else:
            from .opening import night_safe_path
            route = night_safe_path(pioneer, task.task_position, obstacles, state)
        if route is None:
            row['rejected'] = 'no_route'
            rows.append(row)
            continue
        outbound = len(route)
        row['outbound'] = outbound
        stand = pioneer.pos if not route else route[-1]
        row['acceptStand'] = {'x': stand.x, 'y': stand.y}
        detail = station_return_detail(pioneer, state, blocked, from_pos=stand)
        back = detail['steps']
        row['returnSteps'] = back
        row['returnReason'] = detail.get('reason')
        row['alreadyAtPost'] = detail.get('alreadyAtPost')
        if back is None:
            row['rejected'] = detail.get('reason') or 'no_return_from_stand'
            rows.append(row)
            continue
        if back == 0 and not detail.get('alreadyAtPost'):
            row['rejected'] = 'return_zero_unexplained'
            rows.append(row)
            continue
        needed = outbound + solve + back + MUSTER_BUFFER
        row['needed'] = needed
        row['neededSource'] = 'outbound+solveEstimate+return+MUSTER_BUFFER'
        row['defenseConstraint'] = dict(
            outbound=outbound, solve=solve, returnSteps=back, buffer=MUSTER_BUFFER,
            eta=eta, ok=wave or (eta is not None and needed < eta),
        )
        if not wave and eta is None:
            row['rejected'] = 'no_threat_eta'
            rows.append(row)
            continue
        if not wave and needed >= eta:
            row['rejected'] = 'defense_time'
            rows.append(row)
            continue
        rows.append(row)
    return rows


def feasible_rows(rows):
    return [row for row in rows if not row.get('rejected')]


def select_feasible_row(rows, reservation):
    feasible = feasible_rows(rows)
    if not feasible:
        return None
    if reservation:
        for row in feasible:
            if row_matches_reservation(row, reservation):
                return row
    feasible.sort(key=lambda row: (0 if row.get('inAcceptRange') else 1, row.get('outbound') or 0,
                                   row['taskType'], row['x'], row['y']))
    return feasible[0]


def sync_reservation(state, rows):
    reservation = reservation_of(state)
    if state.phase_task:
        if reservation:
            reservation['stage'] = 'active'
            state.policy_memory[RESERVATION_KEY] = reservation
        return reservation_of(state)
    if not reservation:
        return None
    stage = reservation.get('stage') or 'approaching'
    if stage == 'active':
        clear_reservation(state, 'phase_task_cleared')
        return None
    if stage == 'accept_pending':
        return reservation
    if not matching_task(state, reservation):
        clear_reservation(state, 'task_gone')
        return None
    matched = next((row for row in rows if row_matches_reservation(row, reservation)), None)
    if matched is None:
        clear_reservation(state, 'task_gone')
        return None
    if matched.get('rejected'):
        clear_reservation(state, matched['rejected'])
        return None
    return reservation


def save_reservation(state, pioneer, row, stage='approaching'):
    previous = reservation_of(state) or {}
    state.policy_memory[RESERVATION_KEY] = dict(
        pioneerId=pioneer.id, taskType=row['taskType'], x=row['x'], y=row['y'],
        sinceRound=previous.get('sinceRound', state.round_no), timeoutRounds=row.get('timeoutRounds'),
        stage=stage, acceptStand=row.get('acceptStand'),
    )


def interrupt_reservation(state, reason, cost=None, extra=None):
    item = reservation_of(state)
    if not item:
        return
    details = dict(reason=reason, cost=cost, reservation=item)
    if extra:
        details.update(extra)
    trace(state, item.get('pioneerId'), 'task_reservation_interrupted',
          '打断任务预约', **details)


def ensure_schedule_buckets(state):
    if not isinstance(getattr(state, '_pioneer_sched', None), dict):
        state._pioneer_sched = {}
    if not isinstance(getattr(state, '_pioneer_intent', None), dict):
        state._pioneer_intent = {}
    return state._pioneer_sched, state._pioneer_intent


def begin_schedule(state, pioneer, blocked, reserved=None):
    ctx, _intent = ensure_schedule_buckets(state)
    ctx.clear()
    ctx.update(
        branches=[], candidates=[], selected=None, outcome=None, source=None,
        intended=None, inAcceptRange=False, acceptReady=False,
        reservation=reservation_of(state),
        defense=defense_snapshot(pioneer, state, blocked) if pioneer else None,
        occupancy=None, codeVersion=SCHEDULER_VERSION, timeFilter=None,
    )
    if pioneer:
        from .economy import defense_occupancy
        occupancy, snap = defense_occupancy(pioneer, state, blocked)
        ctx['occupancy'] = occupancy
        if isinstance(ctx.get('defense'), dict):
            ctx['defense']['occupancy'] = occupancy
            ctx['defense']['atGun'] = snap.get('atGun')
        ctx['candidates'] = evaluate_task_candidates(pioneer, state, blocked, reserved)
        ctx['reservation'] = sync_reservation(state, ctx['candidates'])
        selected = select_feasible_row(ctx['candidates'], ctx['reservation'])
        ctx['selected'] = selected
        if selected:
            ctx['inAcceptRange'] = bool(selected.get('inAcceptRange'))
            ctx['timeFilter'] = dict(
                outbound=selected.get('outbound'), outboundSource=selected.get('outboundSource'),
                solveEstimate=selected.get('solveEstimate'), solveSource=selected.get('solveSource'),
                returnSteps=selected.get('returnSteps'), returnSource=selected.get('returnSource'),
                returnReason=selected.get('returnReason'), alreadyAtPost=selected.get('alreadyAtPost'),
                acceptStand=selected.get('acceptStand'),
                available=selected.get('available'), availableSource=selected.get('availableSource'),
                needed=selected.get('needed'), neededSource=selected.get('neededSource'),
                timeoutRounds=selected.get('timeoutRounds'),
                taskConstraint=selected.get('taskConstraint'),
                defenseConstraint=selected.get('defenseConstraint'),
                timeoutNote='timeoutRounds是平台从领取起算的最长时限，不是预计解题耗时',
            )
    return ctx


def add_branch(state, name):
    ctx, _ = ensure_schedule_buckets(state)
    ctx.setdefault('branches', []).append(name)


def mark_outcome(state, source, command, outcome):
    ctx, intent = ensure_schedule_buckets(state)
    ctx['source'] = source
    ctx['intended'] = command
    ctx['outcome'] = outcome
    intent.clear()
    intent.update(command=command, source=source, outcome=outcome)


def bump_streak(state, kind):
    streaks = state.policy_memory.setdefault(TASK_STREAK_KEY, {})
    if kind == 'shop':
        streaks['shop'] = streaks.get('shop', 0) + 1
        streaks['defense'] = 0
    elif kind == 'defense':
        streaks['defense'] = streaks.get('defense', 0) + 1
        streaks['shop'] = 0
    else:
        streaks['shop'] = 0
        streaks['defense'] = 0
    return streaks


def shop_progress_stalled(pioneer, state):
    job = state.worker_item_jobs.get(pioneer.id) if state.worker_item_jobs else None
    pos = (pioneer.pos.x, pioneer.pos.y)
    pack = tuple(sorted(str(item) for item in pioneer.backpack))
    signature = dict(
        jobKind=None if not job else job.get('kind'),
        item=None if not job else job.get('item'),
        pos=list(pos), backpack=list(pack),
        phaseTask=bool(state.phase_task),
        reservation=None if not reservation_of(state) else (
            reservation_of(state).get('taskType'), reservation_of(state).get('x'),
            reservation_of(state).get('y')),
    )
    prev = state.policy_memory.get(SHOP_PROGRESS_KEY) or {}
    if prev.get('signature') == signature:
        stalled = int(prev.get('stallRounds') or 0) + 1
    else:
        stalled = 0
    state.policy_memory[SHOP_PROGRESS_KEY] = dict(
        signature=signature, stallRounds=stalled, round=state.round_no)
    if stalled >= SHOP_STALL_ROUNDS:
        return True, stalled
    return False, stalled


def reset_shop_progress(state):
    state.policy_memory.pop(SHOP_PROGRESS_KEY, None)


def pioneer_task_commitment(pioneer, state, blocked, reserved=None):
    rows = evaluate_task_candidates(pioneer, state, blocked, reserved)
    reservation = sync_reservation(state, rows)
    selected = select_feasible_row(rows, reservation)
    return dict(
        reserved=bool(reservation),
        feasible=bool(selected),
        inAcceptRange=bool(selected and selected.get('inAcceptRange')),
        selected=selected, rows=rows, reservation=reservation,
    )


def classify_shop_stall(pioneer, state):
    from .brain import item_cost
    gold = state.team_our.gold_num if state.team_our else 0
    cost = item_cost('WeaponUpgradeVoucher1', state)
    holding = any(isinstance(item, str) and 'WeaponUpgradeVoucher' in item for item in pioneer.backpack)
    if gold < cost and not holding:
        return 'gold_insufficient'
    results = state.last_round_role_action_results or {}
    if results.get(pioneer.id) is False:
        return 'action_rejected'
    last = (state.last_sent_command or {}).get(pioneer.id) or {}
    if last.get('action') == 'move' and results.get(pioneer.id) is True:
        return 'route_blocked'
    return 'no_progress'


def apply_task_choice(pioneer, state, blocked, reserved, row):
    from .decision_log import selected as selected_cmd
    add_branch(state, 'task_committed')
    target = Pos(row['x'], row['y'])
    if row.get('inAcceptRange'):
        save_reservation(state, pioneer, row, stage='accept_pending')
        cmd = {'action': 'acceptTask'}
        mark_outcome(state, 'acceptTask', cmd, 'accept_ready')
        bump_streak(state, 'task')
        reset_shop_progress(state)
        return True, selected_cmd(state, pioneer.id, cmd, '已在领取范围且无更高优先阻塞，当轮接取')
    save_reservation(state, pioneer, row, stage='approaching')
    from .brain import is_day_round
    if is_day_round(state.round_no):
        step = move_towards(pioneer.pos, target, blocked | reserved,
                            state.map_info.width, state.map_info.height)
    else:
        # 夜里从基地后方绕去任务点，避开正面和机器人。
        from .opening import night_safe_path
        path = night_safe_path(pioneer, target, blocked | reserved, state)
        step = path[0] if path else None
    if step:
        reserved.add((step.x, step.y))
        cmd = {'action': 'move', 'targetPos': [{'x': step.x, 'y': step.y}]}
        mark_outcome(state, 'move_to_task', cmd, 'en_route')
        bump_streak(state, 'task')
        reset_shop_progress(state)
        return True, selected_cmd(state, pioneer.id, cmd, '前往已预约任务点')
    return False, None


def emit_scheduler_log(state, commands):
    pioneer = None
    if state.team_our:
        pioneer = next((r for r in state.team_our.roles if r.role_type == 'pioneer'), None)
    ctx, intent = ensure_schedule_buckets(state)
    final = commands.get(pioneer.id) if pioneer and commands else None
    overwritten = None
    intended_cmd = intent.get('command')
    if intended_cmd and intended_cmd.get('action') == 'acceptTask':
        if not final or final.get('action') != 'acceptTask':
            overwritten = dict(
                intended=intended_cmd, final=final,
                reason='acceptTask_dropped_after_merge',
                overwrittenBy=None if not final else final.get('action'),
            )
            trace(state, None if pioneer is None else pioneer.id, 'accept_overwritten',
                  '领取当轮 acceptTask 在合并后消失', intended=intended_cmd, final=final,
                  overwrittenBy=None if not final else final.get('action'))
    streaks = (state.policy_memory or {}).get(TASK_STREAK_KEY) or {}
    from .brain import is_day_round
    phase = 'day' if is_day_round(state.round_no) else 'night'
    job = None
    if pioneer and state.worker_item_jobs:
        job = state.worker_item_jobs.get(pioneer.id)
    reservation = reservation_of(state)
    occupancy = ctx.get('occupancy')
    title = '【调度】开拓者 %s %s' % (
        (final or {}).get('action') or ctx.get('outcome') or 'idle',
        ctx.get('source') or '',
    )
    emit_stderr(
        MARKER, 'scheduler', state.round_no, title=title.strip(),
        codeVersion=SCHEDULER_VERSION,
        dayNight=phase,
        pioneerId=None if pioneer is None else pioneer.id,
        pos=None if pioneer is None else {'x': pioneer.pos.x, 'y': pioneer.pos.y},
        health=None if pioneer is None else pioneer.health,
        phaseTaskPresent=bool(state.phase_task),
        reservation=reservation,
        occupancy=occupancy,
        shopJob=None if not job else {'kind': job.get('kind'), 'item': job.get('item')},
        branches=ctx.get('branches') or [],
        returnedBranch=ctx.get('source'),
        outcome=ctx.get('outcome'),
        intendedAction=None if not intended_cmd else intended_cmd.get('action'),
        finalAction=None if not final else final.get('action'),
        actionSource=ctx.get('source'),
        candidates=ctx.get('candidates') or [],
        shopOccupyStreak=streaks.get('shop', 0),
        defenseOccupyStreak=streaks.get('defense', 0),
        inAcceptRange=bool(ctx.get('inAcceptRange')),
        acceptReady=bool(ctx.get('outcome') == 'accept_ready' and not overwritten),
        acceptOverwritten=overwritten,
        defense=ctx.get('defense'),
        timeFilter=ctx.get('timeFilter'),
        sessionBound=bool(scheduler_task_session(state)),
        shopStallRounds=(state.policy_memory.get(SHOP_PROGRESS_KEY) or {}).get('stallRounds'),
        shopStallThreshold=SHOP_STALL_ROUNDS,
    )
