from django.test import SimpleTestCase

from apps.domain_trust.delivery import inspect_delivery_headers


class DeliveryProofTests(SimpleTestCase):
    def test_jfapps_style_message_flags_missing_dkim(self):
        headers = """From: Tanshuku <contato@example.com>
To: user@gmail.com
Return-Path: <contato@example.com>
Authentication-Results: mx.google.com;
 spf=pass smtp.mailfrom=contato@example.com;
 dmarc=pass (p=NONE) header.from=example.com
"""
        state = inspect_delivery_headers("example.com", headers)
        self.assertEqual(state["spf"], "pass")
        self.assertEqual(state["dmarc"], "pass")
        self.assertEqual(state["dkim"], "unknown")
        self.assertEqual(state["status"], "dkim_missing")
        self.assertFalse(state["delivery_auth_ready"])

    def test_aligned_spf_dkim_dmarc_is_verified(self):
        headers = """From: Example <hello@example.com>
To: user@gmail.com
Return-Path: <bounce@example.com>
Authentication-Results: mx.google.com;
 spf=pass smtp.mailfrom=bounce@example.com;
 dkim=pass header.i=@example.com header.s=s1 header.b=abc;
 dmarc=pass header.from=example.com
DKIM-Signature: v=1; a=rsa-sha256; d=example.com; s=s1; b=abc
"""
        state = inspect_delivery_headers("example.com", headers)
        self.assertEqual(state["status"], "verified")
        self.assertTrue(state["delivery_auth_ready"])
        self.assertTrue(state["spf_aligned"])
        self.assertTrue(state["dkim_aligned"])
        self.assertTrue(state["dmarc_pass"])
        self.assertEqual(state["dkim_selector"], "s1")

    def test_dkim_pass_from_other_domain_is_not_verified(self):
        headers = """From: Example <hello@example.com>
Authentication-Results: mx.google.com;
 spf=pass smtp.mailfrom=bounce@example.com;
 dkim=pass header.i=@mailer.invalid header.s=x1;
 dmarc=pass header.from=example.com
"""
        state = inspect_delivery_headers("example.com", headers)
        self.assertEqual(state["status"], "dkim_unaligned")
        self.assertFalse(state["delivery_auth_ready"])
