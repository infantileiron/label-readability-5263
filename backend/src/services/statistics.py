from math import comb, exp, fsum, lgamma, log, log1p


def exact_mcnemar(old_only, new_only):
    discordant = old_only + new_only
    tail = sum(comb(discordant, i) for i in range(min(old_only, new_only) + 1))
    return min(1.0, (2 * tail) / (1 << discordant))


def direction_interval(new_only, discordant, confidence):
    """Invert equal-tailed binomial tests for P(new wins | discordant)."""
    if not discordant:
        return None
    tail = (100 - confidence) / 200
    log_factorial = [lgamma(i + 1) for i in range(discordant + 1)]

    def invert(start, stop, increasing):
        terms = [(i, log_factorial[-1] - log_factorial[i] - log_factorial[discordant - i])
                 for i in range(start, stop)]
        left, right = 0.0, 1.0
        for _ in range(53):
            mid = min((left + right) / 2, 1 - 2 ** -53)
            log_p, log_q = log(mid), log1p(-mid)
            mass = fsum(exp(weight + i * log_p + (discordant - i) * log_q) for i, weight in terms)
            if (mass < tail) == increasing:
                left = mid
            else:
                right = mid
        return (left + right) / 2

    lower = invert(new_only, discordant + 1, True) if new_only else 0.0
    upper = invert(0, new_only + 1, False) if new_only < discordant else 1.0
    return [lower, upper]


def _included(pairs):
    """纳入统计的配对：excluded 为真（缺测/段冲突/未齐段）一律剔除。

    历史手录数据没有 excluded 字段，按纳入处理，保证原来手录批次照常复验。
    """
    return [row for row in pairs if not row.get('excluded', False)]


def summarize(pairs):
    included = _included(pairs)
    both_ok = old_only = new_only = neither = 0
    for row in included:
        old, new = row['old_ok'], row['new_ok']
        if old is None or new is None:
            continue
        if old and new:
            both_ok += 1
        elif old:
            old_only += 1
        elif new:
            new_only += 1
        else:
            neither += 1
    complete = both_ok + old_only + new_only + neither
    return {
        'total': len(included), 'registered': len(pairs),
        'excluded': len(pairs) - len(included),
        'complete': complete, 'missing': len(included) - complete,
        'both_ok': both_ok, 'old_only': old_only, 'new_only': new_only, 'neither': neither,
        'old_success': both_ok + old_only, 'new_success': both_ok + new_only,
        'delta_pp': (new_only - old_only) * 100 / complete if complete else None,
    }


def analyze(pairs, confidence):
    result = summarize(pairs)
    included = _included(pairs)
    b, c, n = result['old_only'], result['new_only'], result['complete']
    missing = [row for row in included if row['old_ok'] is None or row['new_ok'] is None]
    excluded = [row for row in pairs if row.get('excluded', False)]
    return {
        **result,
        'old_rate': result['old_success'] / n if n else None,
        'new_rate': result['new_success'] / n if n else None,
        'discordant': b + c,
        'p_value': exact_mcnemar(b, c) if n else None,
        'new_win_share': c / (b + c) if b + c else None,
        'direction_interval': direction_interval(c, b + c, confidence),
        'missing_pairs': missing,
        'excluded_pairs': [
            {'visitor_code': row['visitor_code'], 'reading_order': row['reading_order'],
             'reason': row.get('exclude_reason') or '未纳入统计'} for row in excluded
        ],
        'reading_orders': {order: summarize([row for row in included if row['reading_order'] == order])
                           for order in ('old_first', 'new_first')},
    }
