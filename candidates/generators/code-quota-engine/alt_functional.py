# Alternative correct solution: buckets as small classes with a common interface, computed functionally.
import math


class _Token:
    def __init__(self, policy, limit):
        self.cap, self.rate, self.held, self.last = limit * 1000, policy["rate"], limit * 1000, None

    def advance(self, t):
        if self.last is not None:
            self.held = min(self.cap, self.held + (t - self.last) * self.rate)
        self.last = t

    def decide(self, t, cost):
        if self.held >= cost * 1000:
            return True, 0
        if cost * 1000 > self.cap:
            return False, None
        return False, math.ceil((cost * 1000 - self.held) / self.rate) if (cost * 1000 - self.held) % self.rate else (cost * 1000 - self.held) // self.rate

    def charge(self, t, cost, denied):
        self.held -= min(cost * 1000, self.held) if denied else cost * 1000

    def remaining(self, t, limit):
        return self.held // 1000


class _Fixed:
    def __init__(self, policy, limit):
        self.window, self.index, self.used = policy["window_ms"], None, 0

    def advance(self, t):
        if t // self.window != self.index:
            self.index, self.used = t // self.window, 0

    def decide(self, t, cost, limit):
        if self.used + cost <= limit:
            return True, 0
        return (False, None) if cost > limit else (False, (self.index + 1) * self.window - t)

    def charge(self, t, cost, denied):
        self.used += cost

    def remaining(self, t, limit):
        return max(0, limit - self.used)


class _Sliding:
    def __init__(self, policy, limit):
        self.window, self.log = policy["window_ms"], []

    def advance(self, t):
        self.log = [e for e in self.log if e[0] > t - self.window]

    def decide(self, t, cost, limit):
        total = sum(c for _, c in self.log)
        if total + cost <= limit:
            return True, 0
        if cost > limit:
            return False, None
        for when, c in sorted(self.log):
            total -= c
            if total <= limit - cost:
                return False, when + self.window - t

    def charge(self, t, cost, denied):
        self.log.append((t, cost))

    def remaining(self, t, limit):
        return max(0, limit - sum(c for _, c in self.log))


class QuotaEngine:
    def __init__(self, policies, allow_users=()):
        self.policies = list(policies)
        self.allow = set(allow_users)
        self.buckets = {}

    def request(self, t_ms, attrs, cost=1):
        if attrs["user"] in self.allow:
            return {"allowed": True, "retry_after_ms": 0, "limited_by": None, "remaining": {}}
        rows = []
        for p in self.policies:
            if p.get("routes") is not None and not any(attrs["route"].startswith(r) for r in p["routes"]):
                continue
            key = attrs[p["per"]] if p["per"] != "global" else None
            limit = (p.get("overrides") or {}).get(key, p["limit"])
            b = self.buckets.get((p["name"], key))
            if b is None:
                b = self.buckets[(p["name"], key)] = {"token": _Token, "fixed": _Fixed, "sliding": _Sliding}[p["kind"]](p, limit)
            b.advance(t_ms)
            ok, retry = b.decide(t_ms, cost) if p["kind"] == "token" else b.decide(t_ms, cost, limit)
            rows.append((p, b, limit, ok, retry))
        allowed = all(r[3] for r in rows)
        limited, retry_after = None, 0
        if not allowed:
            rejecting = [r for r in rows if not r[3]]
            worst = max(rejecting, key=lambda r: (r[4] is None, r[4] or 0, -rows.index(r)))
            limited, retry_after = worst[0]["name"], worst[4]
        for p, b, limit, ok, retry in rows:
            if allowed or p.get("charge_denied", False):
                b.charge(t_ms, cost, not allowed)
        return {"allowed": allowed, "retry_after_ms": retry_after, "limited_by": limited,
                "remaining": {p["name"]: b.remaining(t_ms, limit) for p, b, limit, _, _ in rows}}
