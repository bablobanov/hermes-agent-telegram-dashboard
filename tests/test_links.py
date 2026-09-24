from telegram_dashboard.links import build_dashboard_link


def test_build_dashboard_link_preserves_base_path_and_profile() -> None:
    link = build_dashboard_link(
        "https://agent.example.com/hermes/",
        path="/sessions",
        profile="worker_1",
    )

    assert link == "https://agent.example.com/hermes/sessions?profile=worker_1"


def test_build_dashboard_link_rejects_unsafe_base_urls() -> None:
    unsafe = (
        "javascript:alert(1)",
        "ftp://agent.example.com",
        "https://user:secret@agent.example.com",
        "https://agent.example.com\r\nX-Test: injected",
    )

    assert all(build_dashboard_link(url) is None for url in unsafe)


def test_build_dashboard_link_rejects_non_allowlisted_paths() -> None:
    assert build_dashboard_link("https://agent.example.com", path="//evil.example") is None
    assert build_dashboard_link("https://agent.example.com", path="/api/env") is None


def test_build_dashboard_link_rejects_invalid_profile_slugs() -> None:
    invalid = ("", "../worker", "worker/name", "worker name", "x" * 65)

    assert all(
        build_dashboard_link("https://agent.example.com", profile=profile) is None
        for profile in invalid
    )
