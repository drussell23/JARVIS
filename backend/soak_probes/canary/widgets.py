"""Canary soak target — throwaway module, safe for the Swarm to rewrite."""


def add(a, b):
    return a + b


def slow_sum(items):
    total = 0
    for x in items:
        total = total + x
    return total


class Counter:
    def __init__(self):
        self.n = 0

    def bump(self):
        self.n = self.n + 1
        return self.n
