"""Bounds of the read_wiki chunk size, kept free of heavy imports so store, server, service and assistant can share them."""

WIKI_READ_CHARS = 8000
# MIN and MAX are policy values. MAX was chosen below the size at which, in a minimal question, the round after one
# read_wiki of MAX Hangul characters stops fitting one unsplit prompt under MAX_CONTEXT; tests/test_wiki_read_setting.py
# (test_max_read_of_hangul_in_a_minimal_question_is_delivered_unsplit) re-measures that one fixture. Larger questions can
# still go to split analysis.
MIN_WIKI_READ_CHARS = 1000
MAX_WIKI_READ_CHARS = 12000


def validate_wiki_read_chars(value):
    if type(value) is not int or not MIN_WIKI_READ_CHARS <= value <= MAX_WIKI_READ_CHARS:
        raise ValueError(f'위키 한 번 읽기 크기는 {MIN_WIKI_READ_CHARS:,}~{MAX_WIKI_READ_CHARS:,}자 사이의 정수여야 합니다.')
    return value
