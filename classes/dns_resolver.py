import logging
import re
import socket
from typing import List, Optional

import aiodns

logger = logging.getLogger(__name__)


class AsyncDNSResolver:
    """
    Async DNS resolver for looking up hostnames to IP addresses.
    """

    def __init__(self, nameservers: Optional[List[str]] = None, timeout: float = 5.0):
        """
        Initialize the DNS resolver.

        Args:
            nameservers: Optional list of DNS nameservers to use (e.g., ['8.8.8.8','1.1.1.1'])
            timeout: DNS query timeout in seconds (default: 5.0)
        """
        self.nameservers = nameservers
        self.timeout = timeout
        self._resolver = None

    def _get_resolver(self):
        """
        Get or create the DNS resolver for the current event loop.
        """
        if self._resolver is None:
            if self.nameservers:
                self._resolver = aiodns.DNSResolver(
                    nameservers=self.nameservers, timeout=self.timeout
                )
            else:
                self._resolver = aiodns.DNSResolver(timeout=self.timeout)
        return self._resolver

    async def resolve(self, hostname: str) -> List[str]:
        """
        Resolve a hostname to a list of IP addresses using async DNS lookup.

        Args:
            hostname: The domain name to resolve (e.g., 'app.rackit.io')

        Returns:
            List of IP addresses as strings
        """
        resolver = self._get_resolver()
        try:
            result = await resolver.query(hostname, "A")
            ip_addresses = [record.host for record in result]
            logger.debug(f"Resolved {hostname} to IPs: {ip_addresses}")
            return ip_addresses
        except aiodns.error.DNSError as e:
            logger.debug(f"DNS resolution failed for {hostname}: {str(e)}")
            return []
        except Exception as e:
            logger.debug(f"Unexpected error during DNS resolution for {hostname}: {str(e)}")
            return []

    async def resolve_with_ttl(self, hostname: str) -> List[tuple]:
        """
        Resolve a hostname and return IP addresses with their TTL values.

        Args:
            hostname: The domain name to resolve

        Returns:
            List of tuples (ip_address, ttl)
        """
        resolver = self._get_resolver()
        try:
            result = await resolver.query(hostname, "A")
            ip_ttl_list = [(record.host, record.ttl) for record in result]
            logger.debug(f"Resolved {hostname} to IPs with TTL: {ip_ttl_list}")
            return ip_ttl_list
        except aiodns.error.DNSError as e:
            logger.debug(f"DNS resolution failed for {hostname}: {str(e)}")
            return []
        except Exception as e:
            logger.debug(f"Unexpected error during DNS resolution for {hostname}: {str(e)}")
            return []

    def set_nameservers(self, nameservers: List[str]):
        """
        Update the DNS nameservers.

        Args:
            nameservers: List of DNS nameserver IPs
        """
        self.nameservers = nameservers
        # Reset resolver so it gets recreated with new nameservers
        self._resolver = None
        logger.debug(f"Updated nameservers to: {nameservers}")

    @staticmethod
    def resolve_nameserver_hosts(nameservers_str: str) -> list[str]:
        """
        Resolve any hostnames in a comma-separated nameserver string to IPs.
        aiodns/c-ares only accepts IPs, not hostnames, as nameservers.
        """
        ip_pattern = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
        resolved = []
        for ns in nameservers_str.split(","):
            ns = ns.strip()
            if not ns:
                continue
            if ip_pattern.match(ns):
                resolved.append(ns)
            else:
                try:
                    ip = socket.gethostbyname(ns)
                    logger.debug(f"Resolved nameserver {ns!r} -> {ip}")
                    resolved.append(ip)
                except socket.gaierror:
                    logger.debug(f"Could not resolve nameserver hostname {ns!r}, skipping.")
        return resolved
