from cmx.enrollment import enroll_link, poll_wait


def test_pending_answers_keep_the_code():
    assert poll_wait(404, None) == 5
    assert poll_wait(503, None) == 10
    assert poll_wait(429, None) == 30
    assert poll_wait(429, 2) == 5          # clamped up to the poll interval
    assert poll_wait(503, 600) == 60       # never longer than a minute
    assert poll_wait(503, 20) == 20


def test_other_answers_are_not_pending():
    for status in (200, 400, 500, 502):
        assert poll_wait(status, None) is None


def test_enroll_link_vector():
    link = enroll_link("https://cm-ex.interaction.works/api/v1/", "abc-def", "site-42")
    assert link == ("dvsiconnect://enroll?v=1&system=cmex"
                    "&server=https%3A%2F%2Fcm-ex.interaction.works&code=ABC-DEF&site=site-42")


def test_enroll_link_keeps_port_and_path_and_encodes():
    link = enroll_link("http://10.0.0.5:8000/cms", " q7 ", None)
    assert link == "dvsiconnect://enroll?v=1&system=cmex&server=http%3A%2F%2F10.0.0.5%3A8000%2Fcms&code=Q7"


def test_enroll_link_needs_http_url_and_code():
    assert enroll_link("ftp://x", "ABC", None) is None
    assert enroll_link("", "ABC", None) is None
    assert enroll_link("http://x", "  ", None) is None


def test_site_is_capped_at_64_characters():
    link = enroll_link("http://x", "C", "s" * 100)
    assert link.endswith("&site=" + "s" * 64)
