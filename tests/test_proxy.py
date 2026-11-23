from __future__ import annotations

import ipaddress
import unittest

from app.proxy import InvalidNetwork, parse_networks, resolve_client_ip


def _nets(*cidrs: str) -> list:
    return [ipaddress.ip_network(cidr) for cidr in cidrs]


class ClientIpTests(unittest.TestCase):
    def test_untrusted_peer_is_returned_verbatim(self) -> None:
        # A direct peer outside TRUSTED_PROXIES must never let a forged
        # X-Forwarded-For override the real socket address.
        resolved = resolve_client_ip(
            peer="203.0.113.9",
            forwarded_for="1.2.3.4",
            trusted_proxies=_nets("10.0.0.0/8"),
        )

        self.assertEqual(resolved, "203.0.113.9")

    def test_trusted_peer_strips_trusted_hops_from_forwarded_for(self) -> None:
        # peer is a trusted proxy; the chain is client, edge-proxy, this-proxy.
        # Trusted hops are stripped from the right, leaving the real client.
        resolved = resolve_client_ip(
            peer="10.0.0.5",
            forwarded_for="203.0.113.9, 10.0.0.4",
            trusted_proxies=_nets("10.0.0.0/8"),
        )

        self.assertEqual(resolved, "203.0.113.9")

    def test_spoofed_left_most_untrusted_value_is_ignored(self) -> None:
        # An attacker prepends a fake address; only the right-most untrusted hop
        # after stripping trusted proxies is believed.
        resolved = resolve_client_ip(
            peer="10.0.0.5",
            forwarded_for="1.1.1.1, 203.0.113.9, 10.0.0.4",
            trusted_proxies=_nets("10.0.0.0/8"),
        )

        self.assertEqual(resolved, "203.0.113.9")


class ParseNetworksTests(unittest.TestCase):
    def test_a_comma_separated_list_is_parsed_ignoring_blanks(self) -> None:
        self.assertEqual(
            parse_networks(" 10.0.0.0/8, ,192.168.1.7/24,2001:db8::/32,203.0.113.9 "),
            _nets("10.0.0.0/8", "192.168.1.0/24", "2001:db8::/32", "203.0.113.9/32"),
        )
        self.assertEqual(parse_networks(""), [])

    def test_the_first_invalid_entry_is_named(self) -> None:
        with self.assertRaises(InvalidNetwork) as error:
            parse_networks("10.0.0.0/8, nonsense, also-bad")
        self.assertEqual(error.exception.entry, "nonsense")
        # Callers that fail closed catch it as the ValueError it always was.
        self.assertIsInstance(error.exception, ValueError)


if __name__ == "__main__":
    unittest.main()
