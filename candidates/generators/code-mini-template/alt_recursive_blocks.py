# Alternative correct solution: the template is first cut into a flat list of tag events, then parsed by
# recursive descent (one function per block kind); expressions compile to closures.
import re


class TemplateError(Exception):
    def __init__(self, message, line):
        super().__init__(message)
        self.line = line


class Undef:
    pass


UNDEF = Undef()
KW = {"and", "or", "not", "true", "false", "none", "in"}
ARITY = {"upper": 0, "lower": 0, "trim": 0, "length": 0, "first": 0, "last": 0, "join": 1, "truncate": 1, "default": 1, "raw": 0}
LEX = re.compile(r"\s+|(-?\d+)|([A-Za-z_]\w*)|'([^']*)'|\"([^\"]*)\"|(==|!=|[|(),.])|(.)", re.S)


def text_of(v):
    if v is UNDEF or v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return ", ".join(text_of(x) for x in v)
    return "[object]"


def truth(v):
    return not (v is UNDEF or v is None or v is False or v == 0 and isinstance(v, int) or v == "" or v == [] or v == {})


def kind_of(v):
    if v is UNDEF or v is None:
        return 0
    return {bool: 1, int: 2, str: 3, list: 4, dict: 5}[type(v)]


def same(a, b):
    if kind_of(a) != kind_of(b):
        return False
    if kind_of(a) == 0:
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    return a == b


class Expr:
    def __init__(self, src, line):
        self.line = line
        self.toks = []
        for m in LEX.finditer(src):
            if m.group(0).isspace():
                continue
            if m.group(6):
                raise TemplateError("bad character", line)
            if m.group(1):
                self.toks.append(("n", m.group(1)))
            elif m.group(2):
                self.toks.append(("k" if m.group(2) in KW else "i", m.group(2)))
            elif m.group(3) is not None or m.group(4) is not None:
                self.toks.append(("s", m.group(3) if m.group(3) is not None else m.group(4)))
            else:
                self.toks.append(("o", m.group(5)))
        self.i = 0
        self.raw_outer = False
        self.fn = self.p_or()
        if self.i != len(self.toks):
            raise TemplateError("trailing tokens", line)

    def at(self, k, v=None):
        return self.i < len(self.toks) and self.toks[self.i][0] == k and (v is None or self.toks[self.i][1] == v)

    def eat(self, k, v=None):
        if not self.at(k, v):
            raise TemplateError("syntax", self.line)
        self.i += 1
        return self.toks[self.i - 1][1]

    def p_or(self):
        f = self.p_and()
        while self.at("k", "or"):
            self.i += 1
            f = (lambda a, b: lambda env: truth(a(env)) or truth(b(env)))(f, self.p_and())
        return f

    def p_and(self):
        f = self.p_not()
        while self.at("k", "and"):
            self.i += 1
            f = (lambda a, b: lambda env: truth(a(env)) and truth(b(env)))(f, self.p_not())
        return f

    def p_not(self):
        if self.at("k", "not"):
            self.i += 1
            f = self.p_not()
            return lambda env: not truth(f(env))
        return self.p_cmp()

    def p_cmp(self):
        f = self.p_chain(True)
        if self.at("o", "==") or self.at("o", "!="):
            op = self.eat("o")
            g = self.p_chain(False)
            if self.at("o", "==") or self.at("o", "!="):
                raise TemplateError("chained comparison", self.line)
            self.raw_outer = False
            return (lambda a, b: lambda env: same(a(env), b(env)) == (op == "=="))(f, g)
        return f

    def p_chain(self, outer):
        f = self.p_prim()
        has_raw = False
        while self.at("o", "|"):
            self.i += 1
            name = self.eat("i")
            if name not in ARITY:
                raise TemplateError("unknown filter", self.line)
            args = []
            if self.at("o", "("):
                self.i += 1
                if not self.at("o", ")"):
                    args.append(self.p_or())
                    while self.at("o", ","):
                        self.i += 1
                        args.append(self.p_or())
                self.eat("o", ")")
            if len(args) != ARITY[name]:
                raise TemplateError("arity", self.line)
            has_raw = has_raw or name == "raw"
            f = (lambda inner, name, args: lambda env: apply(name, inner(env), [a(env) for a in args]))(f, name, args)
        if outer and self.toks and self.i == len(self.toks):
            self.raw_outer = has_raw
        elif outer:
            self.raw_outer = has_raw
        return f

    def p_prim(self):
        if self.at("n"):
            v = int(self.eat("n"))
            return lambda env: v
        if self.at("s"):
            v = self.eat("s")
            return lambda env: v
        for word, val in (("true", True), ("false", False), ("none", None)):
            if self.at("k", word):
                self.i += 1
                return lambda env, val=val: val
        if self.at("o", "("):
            self.i += 1
            f = self.p_or()
            self.eat("o", ")")
            return f
        segs = [self.eat("i")]
        while self.at("o", "."):
            self.i += 1
            if self.at("n") and self.toks[self.i][1].isdigit():
                segs.append(self.eat("n"))
            else:
                segs.append(self.eat("i"))
        return lambda env: walk(segs, env)


def walk(segs, env):
    v = env[0].get(segs[0], env[1].get(segs[0], UNDEF)) if segs[0] in env[0] else env[1].get(segs[0], UNDEF)
    for s in segs[1:]:
        if isinstance(v, dict):
            v = v.get(s, UNDEF)
        elif isinstance(v, list) and s.isdigit() and int(s) < len(v):
            v = v[int(s)]
        else:
            return UNDEF
    return v


def apply(name, v, args):
    if name in ("upper", "lower", "trim"):
        t = text_of(v)
        return t.upper() if name == "upper" else t.lower() if name == "lower" else t.strip()
    if name == "length":
        return len(v) if isinstance(v, (str, list, dict)) else 0
    if name in ("first", "last"):
        return (v[0] if name == "first" else v[-1]) if isinstance(v, list) and v else UNDEF
    if name == "join":
        return text_of(args[0]).join(text_of(x) for x in v) if isinstance(v, list) else text_of(v)
    if name == "truncate":
        t = text_of(v)
        return t if len(t) <= args[0] else t[:args[0] - 3] + "..."
    if name == "default":
        return args[0] if v is UNDEF or v is None else v
    return v


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def events(template):
    """[(kind, line, inner)] with kind text/out/block/comment; dashes already applied to neighbouring text."""
    out, pos = [], 0
    while True:
        m = re.compile(r"\{[{%#]").search(template, pos)
        out.append(["text", 0, template[pos:m.start() if m else len(template)]])
        if not m:
            return out
        line = template.count("\n", 0, m.start()) + 1
        closer = {"{{": "}}", "{%": "%}", "{#": "#}"}[m.group(0)]
        end = template.find(closer, m.end())
        if end < 0:  # reported when the parser reaches it, so that earlier faulty tags win
            out.append(["error", line, ""])
            return out
        inner = template[m.end():end]
        pos = end + 2
        if inner.startswith("-"):
            inner = inner[1:]
            out[-1][2] = out[-1][2].rstrip()
        if inner.endswith("-"):
            inner = inner[:-1]
            pos = len(template) - len(template[pos:].lstrip())
        out.append([{"{{": "out", "{%": "block", "{#": "comment"}[m.group(0)], line, inner])


class Parser:
    def __init__(self, evs):
        self.evs, self.i = evs, 0

    def body(self, closers):
        """Parse nodes until a block tag whose first word is in closers; return (nodes, closer word, line)."""
        nodes = []
        while self.i < len(self.evs):
            kind, line, inner = self.evs[self.i]
            self.i += 1
            if kind == "error":
                raise TemplateError("unclosed", line)
            if kind == "text":
                nodes.append(("text", inner))
            elif kind == "comment":
                pass
            elif kind == "out":
                e = Expr(inner, line)
                if not e.toks:
                    raise TemplateError("empty", line)
                nodes.append(("out", e.fn, e.raw_outer))
            else:
                words = inner.split(None, 1)
                head, rest = (words[0], words[1] if len(words) > 1 else "") if words else ("", "")
                if head in closers:
                    return nodes, head, rest, line
                if head == "if":
                    nodes.append(self.parse_if(rest, line))
                elif head == "for":
                    nodes.append(self.parse_for(rest, line))
                elif head == "set":
                    m = re.match(r"\s*([A-Za-z_]\w*)\s*=(.*)$", rest, re.S)
                    if not m or m.group(1) in KW:
                        raise TemplateError("bad set", line)
                    nodes.append(("set", m.group(1), Expr(m.group(2), line).fn))
                else:
                    raise TemplateError("bad tag", line)
        return nodes, None, None, None

    def parse_if(self, cond, line):
        branches, alt = [], None
        nodes, closer, rest, cline = self.body({"elif", "else", "endif"})
        branches.append((Expr(cond, line).fn, nodes))
        while closer == "elif":
            nodes, closer, rest2, cline2 = self.body({"elif", "else", "endif"})
            branches.append((Expr(rest, cline).fn, nodes))
            rest, cline = rest2, cline2
        if closer == "else":
            if rest.strip():
                raise TemplateError("else", cline)
            alt, closer, rest, cline = self.body({"endif"})
        if closer is None:
            raise TemplateError("open if", line)
        if rest.strip():
            raise TemplateError("endif", cline)
        return ("if", branches, alt)

    def parse_for(self, head, line):
        m = re.match(r"\s*([A-Za-z_]\w*)\s+in\s+(.*)$", head, re.S)
        if not m or m.group(1) in KW:
            raise TemplateError("bad for", line)
        src = Expr(m.group(2), line).fn
        body, closer, rest, cline = self.body({"else", "endfor"})
        alt = None
        if closer == "else":
            if rest.strip():
                raise TemplateError("else", cline)
            alt, closer, rest, cline = self.body({"endfor"})
        if closer is None:
            raise TemplateError("open for", line)
        if rest.strip():
            raise TemplateError("endfor", cline)
        return ("for", m.group(1), src, body, alt)


def run(nodes, env, out):
    for node in nodes:
        if node[0] == "text":
            out.append(node[1])
        elif node[0] == "out":
            t = text_of(node[1](env))
            out.append(t if node[2] else esc(t))
        elif node[0] == "set":
            env[0][node[1]] = node[2](env)
        elif node[0] == "if":
            for cond, body in node[1]:
                if truth(cond(env)):
                    run(body, env, out)
                    break
            else:
                if node[2] is not None:
                    run(node[2], env, out)
        else:
            _, name, src, body, alt = node
            v = src(env)
            items = list(v) if isinstance(v, (list, dict)) else []
            before = dict(env[0])
            if not items and alt is not None:
                run(alt, env, out)
            for i, item in enumerate(items):
                env[0][name] = item
                env[0]["loop"] = {"index": i + 1, "index0": i, "first": i == 0, "last": i == len(items) - 1, "length": len(items)}
                run(body, env, out)
            env[0].clear()
            env[0].update(before)


def render(template, ctx):
    parser = Parser(events(template))
    nodes, closer, rest, line = parser.body({"elif", "else", "endif", "endfor"})
    if closer is not None:
        raise TemplateError("stray closer", line)
    out = []
    run(nodes, ({}, ctx), out)
    return "".join(out)
