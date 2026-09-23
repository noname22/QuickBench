class QuotaEngine:
    def __init__(self, policies, allow_users=()):
        self.policies = [dict(p) for p in policies]
        self.allow_users = set(allow_users)
        self.buckets = {}  # (policy name, key) -> state dict

    # ---- per-kind helpers ------------------------------------------------------------------------------
    def _bucket(self, policy, key, limit):
        state = self.buckets.get((policy["name"], key))
        if state is None:
            kind = policy["kind"]
            if kind == "token":
                state = {"held": limit * 1000, "last": None}
            elif kind == "fixed":
                state = {"index": None, "used": 0}
            else:
                state = {"log": []}
            self.buckets[(policy["name"], key)] = state
        return state

    @staticmethod
    def _sliding_sum(state, t_ms, window):
        state["log"] = [(t, c) for t, c in state["log"] if t > t_ms - window]
        return sum(c for _, c in state["log"])

    def _prepare(self, policy, state, t_ms, limit):
        """Advance the bucket to t_ms; return (accepts, retry_after_ms)."""
        kind = policy["kind"]
        if kind == "token":
            if state["last"] is not None:
                state["held"] = min(limit * 1000, state["held"] + (t_ms - state["last"]) * policy["rate"])
            state["last"] = t_ms
            if state["held"] >= cost_milli(self.cost):
                return True, 0
            if self.cost > limit:
                return False, None
            need = self.cost * 1000 - state["held"]
            return False, -(-need // policy["rate"])
        if kind == "fixed":
            index = t_ms // policy["window_ms"]
            if state["index"] != index:
                state["index"], state["used"] = index, 0
            if state["used"] + self.cost <= limit:
                return True, 0
            if self.cost > limit:
                return False, None
            return False, (index + 1) * policy["window_ms"] - t_ms
        window = policy["window_ms"]
        total = self._sliding_sum(state, t_ms, window)
        if total + self.cost <= limit:
            return True, 0
        if self.cost > limit:
            return False, None
        entries = sorted(state["log"])
        last_dropped = None
        for entry in entries:
            if total <= limit - self.cost:
                break
            total -= entry[1]
            last_dropped = entry
        return False, last_dropped[0] + window - t_ms

    def _charge(self, policy, state, t_ms, denied):
        kind = policy["kind"]
        if kind == "token":
            state["held"] -= min(self.cost * 1000, state["held"]) if denied else self.cost * 1000
        elif kind == "fixed":
            state["used"] += self.cost
        else:
            state["log"].append((t_ms, self.cost))

    def _remaining(self, policy, state, t_ms, limit):
        kind = policy["kind"]
        if kind == "token":
            return state["held"] // 1000
        if kind == "fixed":
            return max(0, limit - state["used"])
        return max(0, limit - self._sliding_sum(state, t_ms, policy["window_ms"]))

    # ---- the request -----------------------------------------------------------------------------------
    def request(self, t_ms, attrs, cost=1):
        if attrs["user"] in self.allow_users:
            return {"allowed": True, "retry_after_ms": 0, "limited_by": None, "remaining": {}}
        self.cost = cost
        applicable = []
        for policy in self.policies:
            routes = policy.get("routes")
            if routes is not None and not any(attrs["route"].startswith(prefix) for prefix in routes):
                continue
            key = "*" if policy["per"] == "global" else attrs[policy["per"]]
            limit = policy.get("overrides", {}).get(key, policy["limit"]) if policy.get("overrides") else policy["limit"]
            state = self._bucket(policy, key, limit)
            accepts, retry = self._prepare(policy, state, t_ms, limit)
            applicable.append((policy, state, limit, accepts, retry))
        allowed = all(a for _, _, _, a, _ in applicable)
        limited_by, retry_after = None, 0
        if not allowed:
            best = None
            for policy, _, _, accepts, retry in applicable:
                if accepts:
                    continue
                rank = (1, 0) if retry is None else (0, retry)
                if best is None or rank > best[0]:
                    best = (rank, policy["name"], retry)
            limited_by, retry_after = best[1], best[2]
        for policy, state, _, _, _ in applicable:
            if allowed:
                self._charge(policy, state, t_ms, False)
            elif policy.get("charge_denied"):
                self._charge(policy, state, t_ms, True)
        remaining = {policy["name"]: self._remaining(policy, state, t_ms, limit) for policy, state, limit, _, _ in applicable}
        return {"allowed": allowed, "retry_after_ms": retry_after, "limited_by": limited_by, "remaining": remaining}


def cost_milli(cost):
    return cost * 1000
