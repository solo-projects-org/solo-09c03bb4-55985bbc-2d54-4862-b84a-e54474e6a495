"""请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class HarvestRequest(BaseModel):
    until_date: str = Field(
        ...,
        alias="until",
        description="采集截止日（含当天），格式 YYYY-MM-DD",
        examples=["2026-10-15"],
    )
    from_date: str | None = Field(
        default=None,
        alias="from",
        description=(
            "起始日（含当天），首次采集另填；之后留空则自动从上次成功"
            "采集的截止日续采"
        ),
        examples=["2026-10-01"],
    )

    model_config = {"populate_by_name": True}


class HarvestResponse(BaseModel):
    status: str = Field(..., description="success / failed")
    from_date: str = Field(..., description="实际使用的起始日（含）")
    until_date: str = Field(..., description="截止日（含）")
    pages: int = Field(..., description="本次成功拉取的页数")
    fetched: int = Field(..., description="拉取到的记录总数（含删除标记）")
    inserted: int
    updated: int
    withdrawn: int = Field(..., description="因删除标记撤下的记录数")
    retries: int = Field(..., description="令牌失效后按原区间重抓的次数（0 或 1）")
    run_id: int
    note: str | None = None
    attributions_recomputed: int = Field(
        0, description="本批触发重算的归属条数（本批书目 + 受影响下级卷册）"
    )


class ProgressResponse(BaseModel):
    last_until: str | None = Field(
        None, description="上次成功采集的截止日（含）；无记录表示尚未采集"
    )
    last_harvest_at: str | None = None


class BibRecordOut(BaseModel):
    identifier: str
    datestamp: str
    status: str
    title: str | None
    creator: str | None
    date: str | None
    identifier_dc: str | None
    updated_at: str


class RunOut(BaseModel):
    id: int
    started_at: str
    finished_at: str | None
    from_date: str
    until_date: str
    status: str
    pages: int
    fetched: int
    inserted: int
    updated: int
    withdrawn: int
    retries: int
    error: str | None


class AttributionOut(BaseModel):
    identifier: str = Field(..., description="被查询的卷册 OAI 标识")
    record_status: str = Field(
        ..., description="书目在架状态：active / withdrawn"
    )
    status: str = Field(
        ...,
        description=(
            "归属状态：root=自身即总目；resolved=已归属到在架总目；"
            "parent_missing=上级本地缺失需补采；"
            "parent_withdrawn=上级已撤下需源馆更正；cycle=引用成环"
        ),
    )
    root_identifier: str | None = Field(
        None, description="在架总目的 OAI 标识；未决时为 null"
    )
    parent_identifier: str | None = Field(
        None, description="直接上级 OAI 标识；总目为 null"
    )
    reason: str | None = Field(None, description="未决/成环时的具体原因")
    chain: list[str] = Field(
        ..., description="逐级上溯链（含起点自身），成环时末尾重复环入口"
    )
    updated_at: str


class UnresolvedAttributionOut(BaseModel):
    identifier: str
    status: str
    parent_identifier: str | None
    root_identifier: str | None
    reason: str | None
    chain: list[str]
    updated_at: str
