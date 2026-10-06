from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.domain_trust.services import inspect_domain


def _response(records):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "Answer": [{"type": 16, "data": f'"{record}"'} for record in records]
    }
    return response


class InspectDomainTests(SimpleTestCase):
    @patch("apps.domain_trust.services.requests.get")
    def test_monitoring_dmarc_is_not_bimi_ready(self, get):
        answers = {
            "example.com": ["v=spf1 include:_spf.example.net -all"],
            "_dmarc.example.com": ["v=DMARC1; p=none; pct=100"],
            "s1._domainkey.example.com": ["v=DKIM1; p=abc123"],
            "default._bimi.example.com": [],
        }

        def side_effect(_url, *, params, **_kwargs):
            return _response(answers.get(params["name"], []))

        get.side_effect = side_effect
        state = inspect_domain("example.com", "s1")
        self.assertEqual(state["dmarc"]["status"], "monitoring")
        self.assertFalse(state["bimi_infrastructure_ready"])
        self.assertFalse(state["gmail_bimi_candidate"])

    @patch("apps.domain_trust.services.requests.get")
    def test_enforced_auth_and_bimi_authority_is_candidate(self, get):
        answers = {
            "example.com": ["v=spf1 include:_spf.example.net -all"],
            "_dmarc.example.com": ["v=DMARC1; p=quarantine; pct=100"],
            "s1._domainkey.example.com": ["v=DKIM1; k=rsa; p=abc123"],
            "default._bimi.example.com": [
                "v=BIMI1; l=; a=https://example.com/.well-known/bimi/mark.pem"
            ],
        }

        def side_effect(_url, *, params, **_kwargs):
            return _response(answers.get(params["name"], []))

        get.side_effect = side_effect
        state = inspect_domain("example.com", "s1")
        self.assertEqual(state["dkim"]["status"], "published")
        self.assertEqual(state["dmarc"]["status"], "protected")
        self.assertTrue(state["bimi_infrastructure_ready"])
        self.assertTrue(state["gmail_bimi_candidate"])
        self.assertEqual(
            state["bimi_authority"],
            "https://example.com/.well-known/bimi/mark.pem",
        )
