def longest_shared_run(traces, k):
    if k == 1:
        best = max(len(t) for t in traces)
        return best, min(list(t) for t in traces if len(t) == best)

    # One suffix automaton over all traces, joined by separators that occur nowhere else (negative, all different).
    # A substring that crosses a separator occurs exactly once, so with k >= 2 it can never qualify.
    seq, owner = [], []
    for index, trace in enumerate(traces):
        seq.extend(trace)
        owner.extend([index] * len(trace))
        seq.append(-1 - index)
        owner.append(-1)

    nxt, link, length, firstpos = [{}], [-1], [0], [-1]
    prefix_state = []
    last = 0
    for pos, symbol in enumerate(seq):
        cur = len(nxt)
        nxt.append({})
        link.append(0)
        length.append(length[last] + 1)
        firstpos.append(pos)
        p = last
        while p != -1 and symbol not in nxt[p]:
            nxt[p][symbol] = cur
            p = link[p]
        if p != -1:
            q = nxt[p][symbol]
            if length[q] == length[p] + 1:
                link[cur] = q
            else:
                clone = len(nxt)
                nxt.append(dict(nxt[q]))
                link.append(link[q])
                length.append(length[p] + 1)
                firstpos.append(firstpos[q])
                while p != -1 and nxt[p].get(symbol) == q:
                    nxt[p][symbol] = clone
                    p = link[p]
                link[q] = link[cur] = clone
        prefix_state.append(cur)
        last = cur

    # In how many different traces does each state occur? Climb the suffix links from every prefix and stop
    # at the first state already seen for this trace (everything above it has been seen as well).
    count = [0] * len(nxt)
    seen_for = [-1] * len(nxt)
    for pos, trace_index in enumerate(owner):
        if trace_index < 0:
            continue
        v = prefix_state[pos]
        while v > 0 and seen_for[v] != trace_index:
            seen_for[v] = trace_index
            count[v] += 1
            v = link[v]

    best = 0
    for v in range(1, len(nxt)):
        if count[v] >= k and length[v] > best:
            best = length[v]
    if best == 0:
        return 0, []
    runs = [seq[firstpos[v] - best + 1:firstpos[v] + 1]
            for v in range(1, len(nxt)) if count[v] >= k and length[v] == best]
    return best, min(runs)
