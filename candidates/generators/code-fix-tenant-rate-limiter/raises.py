class RateLimiter:
    def __init__(self, clock):
        pass

    def __getattr__(self, name):
        def method(*args, **kwargs):
            raise ValueError("not implemented")
        return method
