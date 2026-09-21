"""Helper: how far from the optimum are the heuristics on candidate generators for the large tests?"""
import random
import time
from find_traps import load


def dp_optimum(jobs):
    horizon = max((j[2] for j in jobs), default=0)
    dp = [0] * (horizon + 1)
    for _, p, d, w in sorted(jobs, key=lambda j: j[2]):
        if p <= d:
            dp[p:d + 1] = [a if a >= b + w else b + w for a, b in zip(dp[p:d + 1], dp[0:d + 1 - p])]
    return sum(j[3] for j in jobs) - max(dp)


def night(seed):
    rng = random.Random(seed)
    jobs = []
    for i in range(200):
        duration = rng.randint(20, 140)
        deadline = rng.choice([480, 900, 1500, 2400, 3300, 4200, 5000]) - rng.randint(0, 40)
        jobs.append(("night-%03d" % i, duration, deadline, duration * rng.randint(80, 120) + rng.randint(0, 500)))
    jobs.append(("whale", 5000, 5000, 400000))
    jobs.append(("hopeless", 4000, 3999, 10 ** 9))
    rng.shuffle(jobs)
    return jobs


if __name__ == "__main__":
    names = ["wrong_ratio_greedy", "wrong_moore_hodgson", "wrong_mh_ratio", "wrong_dp_input_order"]
    fns = {n: load(n) for n in names}
    for seed in (2025, 1, 2, 3, 4):
        jobs = night(seed)
        t = time.time()
        opt = dp_optimum(jobs)
        print(seed, opt, round(time.time() - t, 2), {n: fn(jobs)[0] - opt for n, fn in fns.items()})
