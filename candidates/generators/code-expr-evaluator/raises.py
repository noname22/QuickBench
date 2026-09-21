class ExprError(Exception):
    def __init__(self, kind="syntax", pos=0):
        super().__init__(kind)
        self.kind, self.pos = kind, pos


def evaluate(src, env):
    raise ExprError("syntax", 0)
