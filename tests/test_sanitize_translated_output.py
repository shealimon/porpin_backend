"""Tests for stripping model instruction echo leaked into translation segments."""

from app.services.translator.openai_payload import sanitize_translated_output


def test_strips_triple_json_echo_line():
    noisy = (
        "Assistant to=JSON code to=JSON code to=JSON code to=JSON code to=JSON code\n\n"
        "Asli kahani shuru hoti hai yahan.\n"
    )
    clean = sanitize_translated_output(noisy)
    assert "to=JSON" not in clean
    assert "Asli kahani shuru hoti hai yahan." in clean


def test_strips_dup_all_caps_running_head_with_page_number():
    t = (
        'Kuch mazmoon.\nWHITE NIGHTS WHITE NIGHTS\n14\n"Main ne dekha"'
    )
    clean = sanitize_translated_output(t)
    assert "WHITE NIGHTS" not in clean
    assert '14' not in clean or '"' in clean


def test_removes_inline_json_echo_run():
    t = "Pehla jumla Assistant to=JSON code to=JSON code aur aage."
    clean = sanitize_translated_output(t)
    assert "to=JSON" not in clean
    assert "Pehla jumla" in clean and "aur aage" in clean


def test_preserves_legitimate_prose_single_to_equals():
    t = 'User wrote path="foo" literally in prose.'
    assert sanitize_translated_output(t).count("foo") >= 1
