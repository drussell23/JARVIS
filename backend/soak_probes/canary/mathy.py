"""Canary soak target — throwaway module, safe for the Swarm to rewrite."""


def factorial(n):
    r = 1
    for i in range(2, n + 1):
        r = r * i
    return r


def is_even(n):
    return n % 2 == 0
