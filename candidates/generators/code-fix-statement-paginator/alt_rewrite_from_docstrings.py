"""Rewrite from the docstrings: entries as tuples kept sorted per account, cursors resolved by position."""
import bisect


class Ledger:
    def __init__(self):
        self._n = 0
        self._book = {}     # account -> sorted list of (ts, seq, amount, memo, tags-tuple)
        self._carry = {}

    def post(self, account, amount, ts, memo="", tags=None):
        if type(amount) is not int or amount == 0:
            raise ValueError("amount")
        self._n += 1
        bisect.insort(self._book.setdefault(account, []), (ts, self._n, amount, memo, tuple(tags or ())))
        return self._n

    def transfer(self, src, dst, amount, ts, memo="", fee_bp=0):
        if type(amount) is not int or amount <= 0 or src == dst or fee_bp < 0:
            raise ValueError("transfer")
        whole, rest = divmod(amount * fee_bp, 10000)
        fee = whole + (1 if rest * 2 >= 10000 else 0)
        self.post(src, -amount, ts, memo)
        self.post(dst, amount, ts, memo)
        if fee > 0:
            self.post(src, -fee, ts, "fee")
        return fee

    def balance(self, account):
        return self._carry.get(account, 0) + sum(e[2] for e in self._book.get(account, ()))

    def balance_at(self, account, ts):
        return self._carry.get(account, 0) + sum(e[2] for e in self._book.get(account, ()) if e[0] <= ts)

    def accounts(self):
        return sorted(set(self._book) | set(self._carry))

    def entry_count(self, account):
        return len(self._book.get(account, ()))

    def statement(self, account, order="asc", cursor=None, limit=3):
        if order != "asc" and order != "desc":
            raise ValueError("order")
        if limit < 1:
            raise ValueError("limit")
        book = self._book.get(account, [])
        total = self._carry.get(account, 0)
        rows = []
        for ts, seq, amount, memo, tags in book:
            total += amount
            rows.append({"seq": seq, "ts": ts, "amount": amount, "memo": memo, "tags": list(tags), "balance": total})
        keys = [(e[0], e[1]) for e in book]
        if order == "asc":
            start = 0
            if cursor is not None:
                ts, seq = map(int, cursor.split(":"))
                start = bisect.bisect_right(keys, (ts, seq))
            chunk = rows[start:start + limit]
            more = start + limit < len(rows)
        else:
            end = len(rows)
            if cursor is not None:
                ts, seq = map(int, cursor.split(":"))
                end = bisect.bisect_left(keys, (ts, seq))
            first = max(end - limit, 0)
            chunk = rows[first:end][::-1]
            more = first > 0
        nxt = "%d:%d" % (chunk[-1]["ts"], chunk[-1]["seq"]) if more and chunk else None
        return chunk, nxt

    def compact(self, account, keep):
        if keep < 0:
            raise ValueError("keep")
        book = self._book.get(account, [])
        n = len(book) - keep
        if n <= 0:
            return 0
        self._carry[account] = self._carry.get(account, 0) + sum(e[2] for e in book[:n])
        self._book[account] = book[n:]
        return n
