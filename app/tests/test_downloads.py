"""The download filename header the photo album and GAHKFlix put on every file they hand back."""

from core.downloads import content_disposition


def test_a_danish_name_survives_in_both_spellings() -> None:
    header = content_disposition("Sommerfest på taget.jpg")
    assert header.startswith('attachment; filename="Sommerfest p_ taget.jpg"; ')
    assert header.endswith("filename*=UTF-8''Sommerfest%20p%C3%A5%20taget.jpg")


def test_quotes_and_backslashes_cannot_break_out_of_the_quoted_name() -> None:
    assert 'filename="a_b_c.txt"' in content_disposition('a"b\\c.txt')


def test_an_empty_name_still_gets_a_fallback() -> None:
    assert content_disposition("").startswith('attachment; filename="download"')
