import backend.core.ouroboros.battle_test.presentation_restraint as PR


def test_glyphs_utf8(monkeypatch):
    monkeypatch.setattr(PR, "_stdout_supports_utf8", lambda: True)
    g = PR.glyphs()
    assert g["action"] == "⏺" and g["result"] == "⎿"
    assert PR.spinner_name() == "dots"


def test_glyphs_ascii_fallback(monkeypatch):
    monkeypatch.setattr(PR, "_stdout_supports_utf8", lambda: False)
    g = PR.glyphs()
    assert g["action"] == "*" and g["result"] == ">"
    assert PR.spinner_name() == "line"


def test_glyphs_none_encoding_is_safe(monkeypatch):
    monkeypatch.setattr(PR, "_stdout_supports_utf8", lambda: False)
    assert PR.glyphs()["action"] in ("*", "⏺")  # never raises
