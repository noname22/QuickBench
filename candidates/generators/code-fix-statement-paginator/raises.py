class Ledger:
    def __getattr__(self, name):
        def method(*args, **kwargs):
            raise ValueError("not implemented")
        return method
