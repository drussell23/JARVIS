"""Package marker so entry points can import the legacy bootstrap.

Making ``scripts`` a package lets the ``ov`` console script delegate into
``scripts.ouroboros_battle_test.main`` (DRY -- one bootstrap, two front-ends)
without duplicating argument parsing. Running the module directly
(``python3 scripts/ouroboros_battle_test.py``) is unaffected.
"""
