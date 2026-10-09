from .models import BatteryProfile, Telemetry


class BatteryMonitor:
    """Voltage-based warning, not a state-of-charge/time estimator."""
    def __init__(self, profile: BatteryProfile):
        self.profile = profile
        self.state = "UNKNOWN"
        self.filtered = None
        self.pending = None
        self.pending_since = 0.0
        self.battery_session = None
        self.critical_latched = False

    def update(self, t: Telemetry, now: float):
        if self.battery_session != t.battery_session_id:
            self.battery_session = t.battery_session_id
            self.filtered = None
            self.pending = None
            self.state = "UNKNOWN"
            self.critical_latched = False
        p = self.profile
        if not t.measurement_valid or t.pack_voltage_v is None or not p.validated:
            self.pending = None
            self.filtered = None
            self.state = "UNKNOWN"
            return self.state
        v = t.pack_voltage_v
        self.filtered = v if self.filtered is None else .3 * v + .7 * self.filtered
        if v <= p.critical_voltage:
            self.critical_latched = True
        if self.critical_latched:
            self.state = "CRITICAL"
            return self.state
        candidate = "NORMAL"
        if self.filtered <= p.replace_voltage:
            candidate = "REPLACE_REQUIRED"
        elif self.filtered <= p.warning_voltage:
            candidate = "WARNING"
        # Clearing a warning needs both hysteresis and dwell.
        threshold = {"WARNING": p.warning_voltage, "REPLACE_REQUIRED": p.replace_voltage}.get(self.state)
        rank = {"UNKNOWN": -1, "NORMAL": 0, "WARNING": 1, "REPLACE_REQUIRED": 2}
        if threshold is not None and rank[candidate] < rank[self.state] and self.filtered <= threshold + p.hysteresis_v:
            candidate = self.state
        if candidate != self.pending:
            self.pending, self.pending_since = candidate, now
        if now - self.pending_since >= p.dwell_seconds:
            self.state = candidate
        return self.state
