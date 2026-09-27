import socket
import unittest
from unittest.mock import Mock, patch
import urllib3
from mangadock.utils import http


def dns(ip):
    version = socket.AF_INET6 if ':' in ip else socket.AF_INET
    return (version, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443))


class SafeAddressTests(unittest.TestCase):
    def test_ipv4_is_preferred_even_when_dns_returns_ipv6_first(self):
        with patch.object(http.socket, 'getaddrinfo', return_value=[dns('2606:4700::1111'), dns('1.1.1.1')]), \
                patch.object(http, '_urllib3_create_connection', return_value=Mock()) as connect:
            connection = http._SafeHTTPSConnection('graphql.anilist.co', timeout=3)
            connection._new_conn()
            self.assertEqual(connect.call_args.args[0], ('1.1.1.1', 443))
            self.assertEqual(connection.host, 'graphql.anilist.co')
            self.assertEqual(connect.call_count, 1)

    def test_failed_address_falls_back_to_next_validated_address(self):
        result = Mock()
        with patch.object(http.socket, 'getaddrinfo', return_value=[dns('2606:4700::1111'), dns('1.1.1.1'), dns('8.8.8.8')]), \
                patch.object(http, '_urllib3_create_connection', side_effect=[OSError('unreachable'), result]) as connect:
            self.assertIs(http._SafeHTTPSConnection('graphql.anilist.co')._new_conn(), result)
            self.assertEqual([call.args[0][0] for call in connect.call_args_list], ['1.1.1.1', '8.8.8.8'])

    def test_ipv6_only_host_remains_supported(self):
        with patch.object(http.socket, 'getaddrinfo', return_value=[dns('2606:4700::1111')]), \
                patch.object(http, '_urllib3_create_connection', return_value=Mock()) as connect:
            http._SafeHTTPSConnection('graphql.anilist.co')._new_conn()
            self.assertEqual(connect.call_args.args[0][0], '2606:4700::1111')

    def test_all_failures_use_retryable_urllib3_error(self):
        with patch.object(http.socket, 'getaddrinfo', return_value=[dns('1.1.1.1'), dns('2606:4700::1111')]), \
                patch.object(http, '_urllib3_create_connection', side_effect=OSError('unreachable')) as connect:
            with self.assertRaises(urllib3.exceptions.NewConnectionError):
                http._SafeHTTPSConnection('graphql.anilist.co')._new_conn()
            self.assertEqual(connect.call_count, 2)

    def test_unsafe_dns_addresses_never_reach_socket(self):
        with patch.object(http.socket, 'getaddrinfo', return_value=[dns('127.0.0.1')]), \
                patch.object(http, '_urllib3_create_connection') as connect:
            with self.assertRaises(ValueError):
                http._SafeHTTPSConnection('graphql.anilist.co')._new_conn()
            connect.assert_not_called()

    def test_dns_is_resolved_once_per_connection_attempt(self):
        with patch.object(http.socket, 'getaddrinfo', return_value=[dns('1.1.1.1'), dns('1.1.1.1')]) as resolver, \
                patch.object(http, '_urllib3_create_connection', side_effect=socket.timeout()):
            with self.assertRaises(urllib3.exceptions.ConnectTimeoutError):
                http._SafeHTTPSConnection('graphql.anilist.co')._new_conn()
            resolver.assert_called_once()
