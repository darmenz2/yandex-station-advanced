"""Silence/resume policy only; it never records, gates or alters audio samples."""
from __future__ import annotations
import math


class ResumeGate:
    def __init__(self, now: float, *, quiet_seconds: float = 5.0,
                 attack_seconds: float = .12, cooldown: float = 15.0):
        self.quiet_seconds, self.attack_seconds, self.cooldown = quiet_seconds, attack_seconds, cooldown
        self.reset(now)

    def reset(self, now: float):
        self.started = now
        self.armed = False
        self.anchor = 0.0
        self.last_trigger = -1e10
        self.triggers = 0
        self.state = 'listening'

    def observe(self, now: float, signal: dict, *, enabled: bool = True, suspended: bool = False) -> bool:
        if not enabled or suspended:
            self.armed = False
            self.started = now
            self.state = 'suspended' if suspended else 'disabled'
            return False
        age = signal.get('last_signal_ago')
        total = float(signal.get('non_silent_seconds') or 0)
        if not math.isfinite(total) or total < 0:
            return False
        quiet = now - self.started >= self.quiet_seconds if age is None else age >= self.quiet_seconds
        if quiet:
            self.armed = True
            self.anchor = total
            self.state = 'silence'
            return False
        # Do not trigger from one click, background noise or isolated notification.
        # The low threshold in SignalMeter is NOT applied as a sound gate.
        if self.armed and age is not None and age <= .25 and total-self.anchor >= self.attack_seconds:
            self.armed = False
            self.state = 'listening'
            if now-self.last_trigger >= self.cooldown:
                self.last_trigger = now
                self.triggers += 1
                return True
        elif not self.armed:
            self.state = 'listening'
        return False

    def snapshot(self):
        return {'state': self.state, 'armed': self.armed, 'quiet_seconds': self.quiet_seconds,
                'attack_ms': round(self.attack_seconds*1000), 'cooldown_seconds': self.cooldown,
                'triggers': self.triggers}


class MaintenancePolicy:
    """Explicit periodic refresh and bounded repair; no acoustic-delay inference.

    Trigger only while the station reports that our presentation is playing.
    A false/unknown playing state, stale state, Alice or a different source
    suspends the policy. Therefore an explicit pause is never undone.
    """
    ALLOWED_INTERVALS = (0, 120, 240, 480, 600)

    def __init__(self, now: float, interval: int = 240):
        self.interval = interval if interval in self.ALLOWED_INTERVALS else 240
        self.recover = True
        self.reset(now)

    def reset(self, now: float):
        self.last_refresh = now
        self.last_attempt = now
        self.repairs = []
        self.periodic_count = 0
        self.recovery_count = 0
        self.last_reason = ''
        self.state = 'waiting'

    def refreshed(self, now: float, reason: str):
        self.last_refresh = self.last_attempt = now
        self.last_reason = reason
        if reason == 'periodic_live_edge':
            self.periodic_count += 1
        if reason == 'http_recovery':
            self.recovery_count += 1

    def observe(self, now: float, *, suspended: bool, playing: bool,
                has_signal: bool, readers: int, ever_sent: bool,
                last_data_age: float, closed_age: float, close_reason: str) -> str:
        if suspended or not playing:
            self.state = 'suspended'
            return ''
        if now-self.last_attempt < 12:
            self.state = 'cooldown'
            return ''
        self.repairs = [t for t in self.repairs if now-t < 60]
        broken = (not readers and ever_sent and last_data_age >= 2 and closed_age >= 2
                  and close_reason in ('peer_disconnected', 'http_write_timeout',
                                       'http_runtime_error', 'finite_response_complete'))
        if self.recover and broken and len(self.repairs) < 3:
            self.repairs.append(now)
            self.last_attempt = now
            self.state = 'repairing'
            return 'http_recovery'
        if broken:
            self.state = 'repair_limit' if len(self.repairs) >= 3 else 'no_reader'
            return ''
        # Periodic refresh is an explicit user-controllable workaround, not a
        # "detected 350 ms delay". Never fake the internal player buffer size.
        if (self.interval and has_signal and readers > 0 and last_data_age < 2
                and now-self.last_refresh >= self.interval):
            self.last_attempt = now
            self.state = 'periodic_refresh'
            return 'periodic_live_edge'
        self.state = 'playing' if has_signal else 'silence'
        return ''

    def snapshot(self, now: float):
        return {'interval_seconds': self.interval, 'recover_http': self.recover,
                'state': self.state, 'periodic_count': self.periodic_count,
                'recovery_count': self.recovery_count, 'last_reason': self.last_reason,
                'next_periodic_in_seconds': round(max(0, self.interval-(now-self.last_refresh))) if self.interval else None,
                'note': 'Periodic refresh is preventative, not an acoustic latency measurement.'}
