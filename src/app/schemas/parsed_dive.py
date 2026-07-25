from pydantic import BaseModel


class DiveGasChangeSchema(BaseModel):
    gas_change_time: int
    po2: float | None
    set_point_type: int


class DiveMixtureSchema(BaseModel):
    end_pressure: float | None
    helium: float
    name: str | None
    oxygen: float
    po2: float
    size: float
    start_pressure: float | None
    transmitter_id: str | None
    type: int
    gas_changes: list[DiveGasChangeSchema]


class DiveSampleSchema(BaseModel):
    time: int
    depth: float
    temperature: float | None
    averaged_temperature: float | None
    ceiling: float | None
    gas_time: float | None
    heading: float | None
    pressure: float | None
    sac_rate: float | None


class ParsedDiveSchema(BaseModel):
    algorithm: int | None
    altitude_mode: int | None
    ascent_mode: int | None
    ascent_time: int | None
    avg_depth: float | None
    battery_level: float | None
    bottom_temperature: float | None
    bottom_time: int | None
    cns_end: float | None
    cns_start: float | None
    cylinder_volume: float | None
    cylinder_work_pressure: float | None
    desaturation_time: int | None
    dive_number: int | None
    diving_days_in_row: int | None
    duration: int | None
    end_pressure: float | None
    end_temperature: float | None
    last_deco_stop_depth: float | None
    max_depth: float | None
    mode: int | None
    olf_end: float | None
    otu_end: float | None
    otu_start: float | None
    personal_mode: int | None
    previous_max_depth: float | None
    sample_interval: int | None
    serial_number: str | None
    software: str | None
    source: str | None
    start_temperature: float | None
    start_time: str | None
    surface_pressure: float | None
    surface_time: int | None
    mixtures: list[DiveMixtureSchema]
    samples: list[DiveSampleSchema]
