class TemplateError(Exception):
    def __init__(self, message="", line=1):
        super().__init__(message)
        self.line = line


def render(template, ctx):
    raise TemplateError("unsupported template", 1)
