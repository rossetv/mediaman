"""Tests for HTML escaping in unsubscribe pages."""

from mediaman.web.routes.subscribers import _unsub_confirm_html, _unsub_html


class TestUnsubscribeHtmlEscaping:
    def test_confirm_html_escapes_email(self):
        malicious = '"><script>alert(1)</script>@evil.com'
        html = _unsub_confirm_html(malicious, "safe-token")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_confirm_html_escapes_token(self):
        malicious_token = '"><script>alert(2)</script>'
        html = _unsub_confirm_html("safe@example.com", malicious_token)
        assert "<script>alert(2)" not in html

    def test_result_html_escapes_message(self):
        malicious = '<img src=x onerror=alert(1)>@evil.com is already unsubscribed.'
        html = _unsub_html(malicious, success=True)
        # The < and > must be escaped so the img tag never opens in the browser.
        assert "<img" not in html
        assert "&lt;img" in html
