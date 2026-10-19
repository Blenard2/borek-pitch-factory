"""Which website addresses may be fetched at all.

Only public HTTP(S) sites on the standard ports. A host is resolved before every request (and
every redirect hop) and refused when any of its addresses is not a public internet address, so
the research can never reach the API's own network, cloud metadata services or localhost.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

Resolver = Callable[..., list[tuple[Any, ...]]]

ALLOWED_PORTS = {"http": 80, "https": 443}
_BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal", "instance-data"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa", ".corp", ".localdomain")


class WebsiteRejected(ValueError):
    """The address may not be fetched. ``code`` is stable and shown to the user."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_url(raw: str) -> str:
    """An absolute, fetchable http(s) URL without credentials, fragment or a non-standard port.

    A bare domain ("example.com") is read as https. IP-address hosts are refused: a company
    website is addressed by its domain.
    """
    value = str(raw or "").strip()
    if not value:
        raise WebsiteRejected("WEBSITE_NOT_PROVIDED", "No website was provided.")
    if any(char.isspace() or ord(char) < 32 for char in value) or len(value) > 2048:
        raise WebsiteRejected("WEBSITE_URL_INVALID", "The website address is not a valid URL.")
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise WebsiteRejected("WEBSITE_URL_INVALID", "The website address is not a valid URL.") from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_PORTS:
        raise WebsiteRejected("WEBSITE_URL_INVALID", "Only http and https websites can be analysed.")
    if parts.username is not None or parts.password is not None:
        raise WebsiteRejected("WEBSITE_URL_INVALID", "Website addresses with credentials are not analysed.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise WebsiteRejected("WEBSITE_URL_INVALID", "The website address has no host name.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise WebsiteRejected("WEBSITE_URL_INVALID", "The website host name is not valid.") from exc
    if port is not None and port != ALLOWED_PORTS[scheme]:
        raise WebsiteRejected("WEBSITE_URL_BLOCKED", "Only websites on the standard ports 80 and 443 are analysed.")
    check_host_name(host)
    return urlunsplit((scheme, host, parts.path or "/", parts.query, ""))


def check_host_name(host: str) -> None:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise WebsiteRejected("WEBSITE_URL_BLOCKED", "Websites addressed by an IP address are not analysed.")
    if "." not in host or host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise WebsiteRejected("WEBSITE_URL_BLOCKED", "Only public internet websites are analysed.")


def is_public_address(address: str) -> bool:
    """A globally routable unicast address (no private, loopback, link-local, CGNAT, reserved...)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast


def resolve_public(host: str, port: int, resolver: Resolver = socket.getaddrinfo) -> list[str]:
    """The host's addresses, refused when any of them is not public (DNS answers can be mixed)."""
    check_host_name(host)
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise WebsiteRejected("WEBSITE_UNREACHABLE", f"The host {host} could not be resolved.") from exc
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        raise WebsiteRejected("WEBSITE_UNREACHABLE", f"The host {host} could not be resolved.")
    if not all(is_public_address(address) for address in addresses):
        raise WebsiteRejected("WEBSITE_URL_BLOCKED", f"The host {host} points to a private or reserved network address.")
    return addresses
