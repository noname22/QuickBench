class TemplateError(Exception):
    def __init__(self, message="", line=0):
        super().__init__(message)
        self.line = line


def render(template, ctx):
    return None
