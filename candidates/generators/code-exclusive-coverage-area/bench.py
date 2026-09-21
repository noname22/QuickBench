import sys, time, importlib
def lcg(seed):
    state = seed
    while True:
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        yield state >> 33
def big(n, seed=12345):
    g = lcg(seed)
    span = 2_000_000_000
    out = []
    for i in range(n):
        x = next(g) % span - span // 2
        y = next(g) % span - span // 2
        if i % 4 == 0:      # long horizontal band
            w, h = next(g) % (span // 2), next(g) % (span // 2000)
        elif i % 4 == 1:    # tall vertical band
            w, h = next(g) % (span // 2000), next(g) % (span // 2)
        else:               # small block
            w, h = next(g) % (span // 300), next(g) % (span // 300)
        x2 = min(x + w, 10**9); y2 = min(y + h, 10**9)
        out.append((x, y, x2, y2) if i % 3 else (x2, y2, x, y))
    return out
if __name__ == "__main__":
    mod = importlib.import_module(sys.argv[1]); n = int(sys.argv[2])
    z = big(n)
    t = time.time(); r = mod.coverage_report(z); print(r, r[1] / r[0], time.time() - t)
