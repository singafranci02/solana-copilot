"""Phase 5 (partial): free social-capture parsers — no network."""

from src.analyzer.social_capture import (
    extract_tg_username, parse_tg_members, extract_domain, parse_whois_creation,
)


def test_extract_tg_username():
    assert extract_tg_username("https://t.me/CupsemSOL") == "CupsemSOL"
    assert extract_tg_username("https://telegram.me/s/some_chan") == "some_chan"
    assert extract_tg_username("@my_channel") == "my_channel"
    assert extract_tg_username("my_channel") == "my_channel"
    assert extract_tg_username("https://x.com/foo") is None
    assert extract_tg_username(None) is None


def test_parse_tg_members():
    # t.me preview uses non-breaking / narrow spaces as thousands separators
    assert parse_tg_members('<div class="tgme_page_extra">12 345 subscribers</div>') == 12345
    assert parse_tg_members("1,234 members, 56 online") == 1234
    assert parse_tg_members("890 subscribers") == 890
    assert parse_tg_members("no count here") is None


def test_extract_domain():
    assert extract_domain("https://www.example.com/path") == "example.com"
    assert extract_domain("http://Foo.IO") == "foo.io"
    assert extract_domain("example.xyz") == "example.xyz"
    assert extract_domain(None) is None


def test_parse_whois_creation():
    import time
    text = "Domain Name: EXAMPLE.COM\nCreation Date: 2020-01-15T00:00:00Z\n"
    age = parse_whois_creation(text)
    assert age is not None
    expected = int((time.time() - time.mktime(time.strptime("2020-01-15", "%Y-%m-%d"))) / 86400)
    assert abs(age - expected) <= 1
    assert parse_whois_creation("Registered on: 15.01.2020") is not None
    assert parse_whois_creation("no date") is None
