"""Rewrite from the docstrings: one flat structure, levels as integer milli-tokens computed on demand."""


class _B:
    __slots__ = ("cap_m", "rate", "level_m", "at")

    def __init__(self, capacity, rate, now):
        self.cap_m = capacity * 1000
        self.rate = rate
        self.level_m = self.cap_m
        self.at = now

    def sync(self, now):
        if now > self.at:
            self.level_m = min(self.cap_m, self.level_m + (now - self.at) * self.rate)
            self.at = now

    def wait(self, cost):
        need = cost * 1000
        if need > self.cap_m:
            return -1
        short = need - self.level_m
        if short <= 0:
            return 0
        q, r = divmod(short, self.rate)
        return q + (1 if r else 0)


class RateLimiter:
    def __init__(self, clock):
        self.clock = clock
        self._t = {}

    def add_tenant(self, name, capacity, rate, user_capacity, user_rate):
        if name in self._t:
            raise ValueError(name)
        for v in (capacity, rate, user_capacity, user_rate):
            if type(v) is not int or v <= 0:
                raise ValueError("positive ints only")
        self._t[name] = {"bucket": _B(capacity, rate, self.clock()), "ucfg": (user_capacity, user_rate),
                         "users": {}, "ok": {}, "allowed": 0, "denied": 0}

    def allow(self, tenant, user, cost=1):
        t = self._t[tenant]
        if cost < 1:
            raise ValueError("cost")
        now = self.clock()
        ub = t["users"].get(user)
        if ub is None:
            ub = t["users"][user] = _B(t["ucfg"][0], t["ucfg"][1], now)
        ub.sync(now)
        t["bucket"].sync(now)
        need = cost * 1000
        if ub.level_m < need or t["bucket"].level_m < need:
            t["denied"] += 1
            return False
        ub.level_m -= need
        t["bucket"].level_m -= need
        t["allowed"] += 1
        t["ok"][user] = t["ok"].get(user, 0) + 1
        return True

    def retry_after_ms(self, tenant, user, cost=1):
        t = self._t[tenant]
        now = self.clock()
        t["bucket"].sync(now)
        w1 = t["bucket"].wait(cost)
        ub = t["users"].get(user)
        if ub is None:
            w2 = -1 if cost > t["ucfg"][0] else 0
        else:
            ub.sync(now)
            w2 = ub.wait(cost)
        if w1 < 0 or w2 < 0:
            return -1
        return max(w1, w2)

    def top_users(self, tenant, n):
        t = self._t[tenant]
        order = sorted(t["ok"], key=lambda u: (-t["ok"][u], u))
        return [(u, t["ok"][u]) for u in order[:n]]

    def stats(self, tenant):
        t = self._t[tenant]
        return {"allowed": t["allowed"], "denied": t["denied"], "users": len(t["users"])}
