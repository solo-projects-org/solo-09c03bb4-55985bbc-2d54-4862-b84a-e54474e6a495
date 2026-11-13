"""分卷归属（isPartOf）解析与逐级归属求解。

约定（见需求）：
- 源馆在 ``dc:relation`` 的多值中**至多**用一项 ``isPartOf: <OAI 标识>``
  声明上级；其余 relation 值忽略。
- 在架书目未声明上级即视为**总目**。
- 逐级上溯：
  * 到达一个在架且未声明上级的记录 -> 归属成功，返回该总目标识；
  * 上级标识在本地目录中不存在（含撤下墓碑之外的完全缺失）-> 上级缺失，需补采；
  * 上级在架状态为 withdrawn -> 上级已撤下，需请源馆更正；
  * 上溯链中出现已访问标识 -> 引用成环。

解析只依赖数据库当前状态，纯只读；落库由 ``db`` 模块负责，保证与采集
同事务提交、失败整体回滚。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

IS_PARTOF_PREFIX = "isPartOf:"

# 归属状态（同时作为 attributions.status 取值）
STATUS_ROOT = "root"              # 自身即总目（在架且未声明上级）
STATUS_RESOLVED = "resolved"      # 逐级上溯到在架总目
STATUS_PARENT_MISSING = "parent_missing"    # 上级标识本地不存在，需补采
STATUS_PARENT_WITHDRAWN = "parent_withdrawn"  # 上级已撤下，需源馆更正
STATUS_CYCLE = "cycle"            # 引用成环


class AttributionError(RuntimeError):
    """归属数据无法解析（如 raw_dc 损坏），交由采集事务回滚。"""


@dataclass
class AttributionResult:
    """单条书目的归属求解结果。"""

    identifier: str                        # 被查询的卷册标识
    status: str                            # 见 STATUS_* 常量
    root_identifier: str | None = None     # 总目标识；未决时为 None
    parent_identifier: str | None = None   # 直接上级（未声明则为 None）
    reason: str | None = None              # 未决/成环时的中文原因
    chain: list[str] = field(default_factory=list)  # 上溯链（含起点自身）

    def as_row(self) -> dict:
        return {
            "identifier": self.identifier,
            "parent_identifier": self.parent_identifier,
            "root_identifier": self.root_identifier,
            "status": self.status,
            "reason": self.reason,
            "chain": json.dumps(self.chain, ensure_ascii=False),
        }


def extract_parent_identifier(raw_dc: str) -> str | None:
    """从原始 oai_dc JSON 的多值 relation 中提取 isPartOf 上级标识。

    - 多值中至多一项 isPartOf；取第一个以 ``isPartOf:`` 开头（大小写不敏感）
      的值，冒号后 trim。其余 relation 值与非 isPartOf 项一律忽略。
    - 冒号后为空视为未声明上级。
    - raw_dc 无法解析时抛 AttributionError（采集期会触发整批回滚）。
    """
    try:
        dc = json.loads(raw_dc) if raw_dc else {}
    except json.JSONDecodeError as exc:
        raise AttributionError(f"raw_dc 不是合法 JSON：{exc}") from exc
    if not isinstance(dc, dict):
        raise AttributionError("raw_dc 结构不是 Dublin Core 字段映射")

    for value in dc.get("relation") or []:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if stripped[: len(IS_PARTOF_PREFIX)].lower() == IS_PARTOF_PREFIX.lower():
            parent = stripped[len(IS_PARTOF_PREFIX):].strip()
            return parent or None
    return None


def _load_record(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT identifier, status, raw_dc FROM bib_records WHERE identifier = ?",
        (identifier,),
    ).fetchone()


def resolve_attribution(conn: sqlite3.Connection, identifier: str) -> AttributionResult:
    """按标识逐级求解归属。

    起点记录不存在时抛 LookupError（API 层转 404）。起点自身是否撤下不
    影响求解——卷册被撤下后仍需知道它原本归属哪个总目。
    """
    start = _load_record(conn, identifier)
    if start is None:
        raise LookupError(identifier)

    chain: list[str] = []
    current_id = identifier
    visited: set[str] = set()

    while True:
        if current_id in visited:
            # 成环：链尾再补上重复的那一环，便于编目员看清环
            chain.append(current_id)
            cycle_nodes = chain[chain.index(current_id):]
            return AttributionResult(
                identifier=identifier,
                status=STATUS_CYCLE,
                parent_identifier=_direct_parent(chain),
                reason=(
                    "引用成环："
                    + " -> ".join(cycle_nodes)
                    + "，请源馆更正 isPartOf 声明"
                ),
                chain=chain,
            )
        visited.add(current_id)
        chain.append(current_id)

        # 跳转前已校验过 current_id 存在（首跳查起点，后续跳查上级），
        # 因此这里必然能取到记录
        row = _load_record(conn, current_id)
        assert row is not None

        parent = extract_parent_identifier(row["raw_dc"])

        if parent is None:
            # 当前记录未声明上级：它就是链顶。
            # 能进入此分支的 withdrawn 行只可能是起点自身——撤下的上级在
            # 上一跳跳转前已被拦截为 parent_withdrawn。
            if current_id == identifier:
                # 在架未声明上级即总目；起点自身撤下时仍按总目返回，
                # 其 withdrawn 状态由 API 响应中的 record_status 体现
                return AttributionResult(
                    identifier=identifier,
                    status=STATUS_ROOT,
                    root_identifier=current_id,
                    parent_identifier=None,
                    reason=(
                        "该记录已撤下，且未声明上级，按总目处理"
                        if row["status"] == "withdrawn" else None
                    ),
                    chain=chain,
                )
            return AttributionResult(
                identifier=identifier,
                status=STATUS_RESOLVED,
                root_identifier=current_id,
                parent_identifier=_direct_parent(chain),
                reason=None,
                chain=chain,
            )

        # 声明了上级：先看上级是否在架，再继续上溯
        parent_row = _load_record(conn, parent)
        if parent_row is None:
            chain.append(parent)
            return AttributionResult(
                identifier=identifier,
                status=STATUS_PARENT_MISSING,
                parent_identifier=parent,
                reason=f"上级 {parent} 本地缺失，需补采该总目或请源馆更正",
                chain=chain,
            )
        if parent_row["status"] == "withdrawn":
            chain.append(parent)
            return AttributionResult(
                identifier=identifier,
                status=STATUS_PARENT_WITHDRAWN,
                parent_identifier=parent,
                reason=f"上级 {parent} 已撤下，请源馆更正 isPartOf 声明",
                chain=chain,
            )
        current_id = parent


def _direct_parent(chain: list[str]) -> str | None:
    """链中至少两跳时，起点的直接上级是第二个节点。"""
    return chain[1] if len(chain) >= 2 else None
