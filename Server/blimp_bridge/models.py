from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class BatteryProfile(Model):
    validated: bool = False
    warning_voltage: float | None = Field(None, gt=0)
    replace_voltage: float | None = Field(None, gt=0)
    critical_voltage: float | None = Field(None, gt=0)
    hysteresis_v: float = Field(0.1, ge=0)
    dwell_seconds: float = Field(3, ge=0)

    @model_validator(mode="after")
    def ordered(self):
        values = (self.critical_voltage, self.replace_voltage, self.warning_voltage)
        if any(v is not None for v in values):
            if any(v is None for v in values) or not values[0] < values[1] < values[2]:
                raise ValueError("Set all voltage thresholds: critical < replace < warning")
        if self.validated and self.warning_voltage is None:
            raise ValueError("Validated battery profile requires calibrated thresholds")
        return self


class VehicleProfile(Model):
    validated: bool = False
    profile_hash: str = "UNCONFIGURED"
    surge_max: float = Field(0, ge=0, le=1)
    yaw_rate_max: float = Field(0, ge=0)
    reverse_supported: bool = False
    expiry_behavior: str | None = None


class Enroll(Model):
    drone_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80)
    battery: BatteryProfile = Field(default_factory=BatteryProfile)
    vehicle: VehicleProfile = Field(default_factory=VehicleProfile)


class Binding(Model):
    expected_binding_epoch: str


class Assign(Binding):
    drone_id: str


class Action(Binding):
    duration_ms: int = Field(ge=100, le=10000)


class Command(Binding):
    server_epoch: str
    action_id: str
    command_seq: int = Field(ge=0, le=2**31-1)
    surge_norm: float = Field(ge=-1, le=1)
    yaw_rate_rad_s: float


class Recovery(Model):
    recovered: Literal[True]
    physically_powered_off: bool = False


class Replacement(Model):
    drone_id: str


class Telemetry(Model):
    protocol_version: Literal[1] = 1
    drone_id: str
    boot_id: str
    telemetry_seq: int = Field(ge=0)
    uptime_ms: int = Field(ge=0)
    armed: bool
    battery_session_id: str
    pack_voltage_v: float | None = Field(None, gt=0, le=100)
    soc_pct: float | None = Field(None, ge=0, le=100)
    measurement_valid: bool = False
    control_session: str | None = None
    last_applied_seq: int | None = None


class MockUpdate(Model):
    pack_voltage_v: float | None = Field(None, gt=0, le=100)
    measurement_valid: bool = True
    battery_session_id: str | None = None


class Settings(Model):
    hardware_arming_enabled: bool = False
    service_uuid: str = "74b70000-5401-4fba-8984-fddff08b0100"
    identity_uuid: str = "74b70001-5401-4fba-8984-fddff08b0100"
    control_uuid: str = "74b70002-5401-4fba-8984-fddff08b0100"
    telemetry_uuid: str = "74b70003-5401-4fba-8984-fddff08b0100"
    ack_uuid: str = "74b70004-5401-4fba-8984-fddff08b0100"
    stale_seconds: float = Field(2, gt=0)
    rpc_timeout: float = Field(2, gt=0, le=10)
    command_ttl_ms: int = Field(500, ge=100, le=2000)
    max_clock_rtt_ms: int = Field(150, ge=1, le=500)
    command_hz: float = Field(10, gt=0, le=30)
