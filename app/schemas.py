"""Pydantic request/response models for the handover API."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

MAX_SAMPLES = 200
MAX_ZONES = 200


class Sample(BaseModel):
    """一个原始采样点：整数秒 + 平面整米坐标。"""

    model_config = ConfigDict(extra="forbid")

    t: StrictInt = Field(description="采样时刻，整数秒，整条轨迹严格递增")
    x: StrictInt = Field(description="平面坐标 x，整米")
    y: StrictInt = Field(description="平面坐标 y，整米")


class Zone(BaseModel):
    """轴对齐矩形管护区；边界不算区内（开矩形）。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200, description="管护区名称")
    x_min: StrictInt = Field(description="矩形 x 下界（开）")
    x_max: StrictInt = Field(description="矩形 x 上界（开）")
    y_min: StrictInt = Field(description="矩形 y 下界（开）")
    y_max: StrictInt = Field(description="矩形 y 上界（开）")

    @model_validator(mode="after")
    def _check_extent(self) -> "Zone":
        if not (self.x_min < self.x_max and self.y_min < self.y_max):
            raise ValueError("管护区必须是轴对齐矩形且 x_min < x_max、y_min < y_max")
        return self


class TrailCreate(BaseModel):
    """登记一条待分段的湿地轨迹。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    samples: list[Sample] = Field(
        min_length=2,
        max_length=MAX_SAMPLES,
        description=f"原始采样点，2..{MAX_SAMPLES} 个，时刻严格递增",
    )
    zones: list[Zone] = Field(
        default_factory=list,
        max_length=MAX_ZONES,
        description=f"管护区列表，0..{MAX_ZONES} 个，可为空",
    )
    error_limit: Decimal = Field(
        ge=0,
        allow_inf_nan=False,
        max_digits=30,
        description="位置误差限值（米）。简化轨迹在任一原采样时刻的偏差不得超过此值。",
    )
    point_limit: StrictInt = Field(
        ge=1,
        description="全程点数上限；各段接缝点只计一次。",
    )

    @field_validator("error_limit")
    @classmethod
    def _limit_precision(cls, value: Decimal) -> Decimal:
        # Exact rational arithmetic is used downstream; keep denominators sane.
        frac = Fraction(format(value, "f"))
        if frac.denominator > 10**18:
            raise ValueError("位置误差限值精度过高（最多 18 位小数）")
        return value

    @model_validator(mode="after")
    def _check_times(self) -> "TrailCreate":
        times = [s.t for s in self.samples]
        if any(later <= earlier for earlier, later in zip(times, times[1:])):
            raise ValueError("采样时刻必须严格递增")
        return self


class SampleOut(Sample):
    index: int = Field(description="该点在原始轨迹中的下标")


class ZoneOut(Zone):
    index: int = Field(description="管护区下标")


class SegmentOut(BaseModel):
    """一个已签发段。落库后不可修改。"""

    trail_id: str
    seq: int = Field(description="段序号，从 1 开始")
    start_index: int = Field(description="段首采样点下标（与上一段接缝，只计一次）")
    end_index: int = Field(description="段尾采样点下标")
    observed_progress: int = Field(description="申请时声明的所见进度")
    required_pins: list[int] = Field(description="本段必留复核点下标")
    points: list[SampleOut] = Field(description="本段简化后保留的原始采样点")
    point_count: int = Field(description="本段点数（含段首）")
    new_point_count: int = Field(description="本段新增点数（不含接缝点）")
    total_points_used: int = Field(
        description="截至本段全轨迹已占用点数（接缝只计一次；已签发点不可回收）"
    )
    point_limit: int = Field(description="登记的全程点数上限")
    remaining_point_budget: int = Field(description="本段签发后剩余可用点数")
    remaining_minimum_points: int = Field(
        description="从本段终点到轨迹终点所需的最少点数（含终点这个接缝点）"
    )
    remaining_new_points: int = Field(
        description="未来至少还要新增的点数（= remaining_minimum_points - 1）"
    )
    minimum_total_points: int = Field(
        description=(
            "按当前进度与本段选择，完成整条轨迹所需的最少总点数："
            "已签发点 + 本段新增点 + 余段最少新增点"
        )
    )
    created_at: str


class SegmentRequest(BaseModel):
    """申请下一段。"""

    model_config = ConfigDict(extra="forbid")

    observed_progress: StrictInt = Field(
        description="客户端所见进度：已签发段的最末端采样点下标；尚未签发时为 0。"
    )
    end_index: StrictInt = Field(description="本段终点采样点下标（必须大于当前进度）")
    required_pins: list[StrictInt] = Field(
        default_factory=list,
        max_length=MAX_SAMPLES,
        description="本段必须保留的复核点下标，必须位于 [当前进度, 终点] 内",
    )


class TrailSummary(BaseModel):
    id: str
    name: str | None
    created_at: str
    error_limit: str
    point_limit: int
    sample_count: int
    zone_count: int
    progress_index: int = Field(description="最新进度（已签发段末端下标，未签发为 0）")
    issued_segment_count: int
    issued_point_count: int = Field(description="已占用点数，接缝只计一次")
    remaining_point_budget: int
    minimum_total_points: int = Field(
        description="按当前进度完成整条轨迹所需的最少总点数（以已签发点为准）"
    )
    can_complete_within_limit: bool


class TrailDetail(TrailSummary):
    samples: list[SampleOut]
    zones: list[ZoneOut]
    segments: list[SegmentOut]


class ValidationProblem(BaseModel):
    loc: list[str]
    msg: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    problems: list[ValidationProblem] | None = None
    trail_id: str | None = None
    current_progress: int | None = None
    point_limit: int | None = None
    issued_point_count: int | None = None
    segment_minimum_points: int | None = None
    remaining_minimum_points: int | None = None
    minimum_total_points: int | None = None


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class Health(BaseModel):
    status: str
