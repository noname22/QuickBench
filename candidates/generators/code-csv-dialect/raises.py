class CsvError(Exception):
    def __init__(self, line=1, col=1):
        super().__init__(f"{line}:{col}")
        self.line, self.col = line, col


def parse(text, dialect):
    raise CsvError(1, 1)
