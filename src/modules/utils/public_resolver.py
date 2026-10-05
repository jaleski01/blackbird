import asyncio
import ipaddress
import socket

from aiohttp.abc import AbstractResolver


class PublicNetworkResolver(AbstractResolver):
    """Resolve public addresses only, including after redirects."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        try:
            address = ipaddress.ip_address(host)
            addresses = [(socket.AF_INET6 if address.version == 6 else socket.AF_INET, str(address))]
        except ValueError:
            records = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                family=family,
                type=socket.SOCK_STREAM,
            )
            addresses = [(record[0], record[4][0]) for record in records]

        resolved = []
        for address_family, address in addresses:
            parsed_address = ipaddress.ip_address(address.split("%", 1)[0])
            if not parsed_address.is_global:
                raise OSError("Requests to non-public network addresses are blocked")
            resolved.append(
                {
                    "hostname": host,
                    "host": str(parsed_address),
                    "port": port,
                    "family": address_family,
                    "proto": socket.IPPROTO_TCP,
                    "flags": 0,
                }
            )
        if not resolved:
            raise OSError("The destination has no public network addresses")
        return resolved

    async def close(self):
        return None
