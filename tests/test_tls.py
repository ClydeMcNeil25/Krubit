import ssl

from krubit.security.tls import system_ssl_context


def test_system_ssl_context_keeps_validation_and_disables_only_strict_extension_rules() -> None:
    context = system_ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.verify_flags & ssl.VERIFY_X509_STRICT == 0
