"""Canary soak target — throwaway module, safe for the Swarm to rewrite."""


def shout(s):
    return s.upper() + "!"


def repeat(s, n):
    out = ""
    for _ in range(n):
        out = out + s
    return out
