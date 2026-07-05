from validation.text import truncate_to_sentence_boundary


def test_truncate_to_sentence_boundary_prefers_period():
    text = "Short sentence. Another useful sentence. Extra trailing text."

    assert truncate_to_sentence_boundary(text, 40) == "Short sentence. Another useful sentence."


def test_truncate_to_sentence_boundary_hard_cuts_when_no_period():
    text = "abcdefghijklmnopqrstuvwxyz"

    assert truncate_to_sentence_boundary(text, 10) == "abcdefghij"
