class CsvError(Exception):
    def __init__(self, line=0, col=0):
        super().__init__(f"{line}:{col}")
        self.line, self.col = line, col


def parse(text, dialect):
    return None
