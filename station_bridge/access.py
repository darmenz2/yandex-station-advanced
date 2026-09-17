"""Explicit, exact-IP approval for source-NAT audio callbacks.

An HTTP peer is not a speaker identity. A relay is suggested only after it
requests an existing resource with the current random URL token. It remains
blocked until the authenticated localhost UI approves a short-lived challenge.
Neither proxy headers nor membership in a private subnet grant access.
"""
from __future__ import annotations
import ipaddress
import secrets
import time
from .protocol import BridgeError

PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(net) for net in
                         ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
APPROVAL_SECONDS = 120


def private_peer(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise BridgeError('Для посредника нужен один частный IPv4-адрес.') from exc
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise BridgeError('Посредник должен иметь частный IPv4-адрес, не публичный или служебный.')
    return str(address)


def record_matches(record: object, device: dict, host: str, fingerprint: str) -> bool:
    """Never carry an approval to another speaker, PC address or certificate."""
    return bool(isinstance(record, dict) and fingerprint and
                record.get('station_ip') == device.get('host') and
                record.get('audio_host') == host and
                record.get('fingerprint') == fingerprint)


class AudioPeerPolicy:
    def __init__(self):
        self.host = ''
        self.station = ''
        self.approved = ''
        self._pending: dict = {}

    def configure(self, host: str, station: str):
        self.host, self.station = host, station
        self.approved = ''
        self.clear_pending()

    def allows(self, peer: str) -> bool:
        return bool(peer and peer in {self.station, self.approved, '127.0.0.1'})

    def restore(self, peer: str):
        peer = private_peer(peer)
        if peer in {self.host, self.station}:
            raise BridgeError('Адрес посредника не должен совпадать с IP ПК или Станции.')
        self.approved = peer
        self.clear_pending()

    def observe(self, peer: str, resource: str) -> bool:
        """Call only AFTER current-token and existing-file validation."""
        if not resource or self.allows(peer) or peer == self.host:
            return False
        try:
            peer = private_peer(peer)
        except BridgeError:
            return False
        now = time.monotonic()
        # Keep one live candidate stable; a second peer cannot replace it.
        if self.pending(resource):
            return False
        self._pending = {'ip': peer, 'resource': resource,
                         'challenge': secrets.token_urlsafe(24),
                         'expires': now + APPROVAL_SECONDS}
        return True

    def pending(self, resource: str, *, challenge: bool = False) -> dict | None:
        current = self._pending
        if not resource or not current or current['resource'] != resource or time.monotonic() >= current['expires']:
            self.clear_pending()
            return None
        result = {'ip': current['ip'], 'expires_in': max(0, round(current['expires'] - time.monotonic()))}
        if challenge:
            result['challenge'] = current['challenge']
        return result

    def approve(self, peer: str, challenge: str, resource: str) -> str:
        current = self.pending(resource, challenge=True)
        if not current or peer != current['ip'] or not secrets.compare_digest(challenge, current['challenge']):
            raise BridgeError('Подтверждение адреса устарело. Повторите проверку звука и используйте новое уведомление.')
        self.restore(peer)
        return self.approved

    def clear_pending(self):
        self._pending = {}

    def revoke(self):
        self.approved = ''
        self.clear_pending()
