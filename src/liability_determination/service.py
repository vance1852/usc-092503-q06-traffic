"""责任认定草案、版本历史、分级签署与送达文本的事务用例。"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, isoformat
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .liability import (
    LEVEL_LABELS,
    MATERIAL_CATEGORIES,
    PARTY_KINDS,
    TRIGGER_REASONS,
    LiabilityValidationError,
    ensure_material_coverage,
    final_review_required,
    liability_level,
    share_text,
    validate_materials,
    validate_party_shares,
    render_service_document,
)
from .storage import initialize, transaction


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

ROLE_PERMISSIONS = {
    "investigator": {"accident.register", "party.register", "draft.write", "draft.submit", "report.read"},
    "reviewer": {"review.sign", "report.read"},
    "chief": {"final.sign", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

EVENT_LABELS = {
    "accident.registered": "登记事故案件",
    "party.registered": "登记当事人",
    "draft.created": "创建责任认定草案",
    "version.edited": "修改尚未提交的草案",
    "version.created": "形成认定新版本",
    "version.submitted": "主办民警提交签署",
    "version.review_signed": "复核人员签署同意",
    "version.returned": "复核人员退回修订",
    "version.final_signed": "负责人终审签署",
    "version.effective": "认定版本完成所需层级并生效",
    "version.superseded": "旧版本被新版本承接",
    "signing.invalidated": "旧签署因版本修订失效",
    "document.generated": "生成送达文本",
}


class LiabilityDeterminationService:
    """在单个 SQLite 连接上提供责任认定全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ---------- 基础辅助 ----------

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    @staticmethod
    def _identifier(value: object, field: str) -> str:
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value.strip()):
            raise ValidationFailed(f"{field} 格式不正确")
        return value.strip()

    @staticmethod
    def _text(value: object, field: str, maximum: int = 2000) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailed(f"{field} 不能为空")
        result = value.strip()
        if len(result) > maximum:
            raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
        return result

    @staticmethod
    def _occurred_at(value: object) -> str:
        text = LiabilityDeterminationService._text(value, "occurred_at", 40)
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationFailed("occurred_at 必须是 ISO 8601 时间") from exc
        return text

    # ---------- 用户与案件 ----------

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_accident(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "accident.register")
        accident_id = self._identifier(raw.get("accident_id"), "accident_id")
        occurred_at = self._occurred_at(raw.get("occurred_at"))
        location = self._text(raw.get("location"), "location", 256)
        summary = self._text(raw.get("summary"), "summary", 4000)
        has_casualty = 1 if bool(raw.get("has_casualty", False)) else 0
        evidence_chain_anomaly = 1 if bool(raw.get("evidence_chain_anomaly", False)) else 0
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO accidents(accident_id,occurred_at,location,summary,has_casualty,"
                    "evidence_chain_anomaly,registered_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (accident_id, occurred_at, location, summary, has_casualty, evidence_chain_anomaly,
                     actor_id, now, now),
                )
                self._audit("accident", accident_id, "accident.registered", actor_id, {
                    "has_casualty": bool(has_casualty),
                    "evidence_chain_anomaly": bool(evidence_chain_anomaly),
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"事故案件已存在: {accident_id}") from exc
        return self.accident(actor_id, accident_id)

    def _accident_row(self, accident_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM accidents WHERE accident_id=?", (accident_id,)).fetchone()
        if row is None:
            raise NotFound(f"事故案件不存在: {accident_id}")
        return row

    def accident(self, actor_id: str, accident_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self._accident_row(accident_id)
        result = dict(row)
        result["has_casualty"] = bool(row["has_casualty"])
        result["evidence_chain_anomaly"] = bool(row["evidence_chain_anomaly"])
        result["parties"] = self.connection.execute(
            "SELECT party_id,name,party_kind,contact FROM accident_parties WHERE accident_id=? ORDER BY party_id",
            (accident_id,),
        ).fetchall()
        result["parties"] = [dict(item) for item in result["parties"]]
        return result

    def add_party(
        self, actor_id: str, accident_id: str, party_id: str, name: str, party_kind: str, contact: str | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "party.register")
        self._accident_row(accident_id)
        party_id = self._identifier(party_id, "party_id")
        name = self._text(name, "name", 64)
        if party_kind not in PARTY_KINDS:
            raise ValidationFailed(f"party_kind 必须是 {sorted(PARTY_KINDS)} 之一")
        contact = contact.strip() if isinstance(contact, str) and contact.strip() else None
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO accident_parties(accident_id,party_id,name,party_kind,contact,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (accident_id, party_id, name, party_kind, contact, now),
                )
                self._audit("accident", accident_id, "party.registered", actor_id, {
                    "party_id": party_id, "name": name, "party_kind": party_kind,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"当事人已存在: {party_id}") from exc
        return {"accident_id": accident_id, "party_id": party_id, "name": name, "party_kind": party_kind}

    # ---------- 草案内容解析 ----------

    def _parties_snapshot(self, accident_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT party_id,name,party_kind,contact FROM accident_parties WHERE accident_id=? ORDER BY party_id",
            (accident_id,),
        ).fetchall()
        if not rows:
            raise ValidationFailed("事故案件尚未登记当事人，无法形成责任认定")
        return [dict(row) for row in rows]

    def _build_content(self, accident_id: str, raw: Mapping[str, Any], *, complete: bool) -> dict[str, Any]:
        parties = self._parties_snapshot(accident_id)
        party_ids = [item["party_id"] for item in parties]
        conclusion = self._text(raw.get("conclusion"), "conclusion", 8000)
        share_rows = raw.get("party_shares")
        if not isinstance(share_rows, list):
            raise ValidationFailed("party_shares 必须是数组")
        try:
            shares = validate_party_shares(party_ids, share_rows)
        except LiabilityValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        try:
            materials = validate_materials(raw.get("materials"))
        except LiabilityValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        if complete:
            try:
                ensure_material_coverage(item.category for item in materials)
            except LiabilityValidationError as exc:
                raise ValidationFailed(str(exc)) from exc
        content = {
            "conclusion": conclusion,
            "parties": parties,
            "party_shares": sorted(
                (
                    {
                        "party_id": party_id,
                        "share_percent": share_text(item["share_percent"]),
                        "basis": item["basis"],
                    }
                    for party_id, item in shares.items()
                ),
                key=lambda item: item["party_id"],
            ),
            "materials": sorted(
                (
                    {
                        "category": item.category,
                        "reference": item.reference,
                        "title": item.title,
                        "detail": item.detail,
                    }
                    for item in materials
                ),
                key=lambda item: (item["category"], item["reference"]),
            ),
        }
        return content

    @staticmethod
    def _content_digest(content: Mapping[str, Any]) -> str:
        return content_digest([content])

    def _draft_row(self, draft_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM liability_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if row is None:
            raise NotFound(f"责任认定草案不存在: {draft_id}")
        return row

    def _current_version(self, draft_row: sqlite3.Row) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM liability_versions WHERE draft_id=? AND version_no=?",
            (draft_row["draft_id"], draft_row["current_version_no"]),
        ).fetchone()
        if row is None:  # 不可能发生，防御性处理
            raise InvalidState("草案当前版本缺失")
        return row

    # ---------- 草案创建 / 编辑 / 修订 ----------

    def create_draft(self, actor_id: str, accident_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "draft.write")
        accident = self._accident_row(accident_id)
        if self.connection.execute(
            "SELECT 1 FROM liability_drafts WHERE accident_id=?", (accident_id,)
        ).fetchone():
            raise Conflict("该事故案件已经存在责任认定草案")
        content = self._build_content(accident_id, raw, complete=False)
        digest = self._content_digest(content)
        draft_id = f"LD-{accident_id}"
        version_id = f"{draft_id}-V1"
        now = self._now()
        final_required = 1 if final_review_required(
            bool(accident["has_casualty"]), bool(accident["evidence_chain_anomaly"])
        ) else 0
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO liability_drafts(draft_id,accident_id,current_version_no,created_by,created_at) "
                "VALUES(?,?,1,?,?)",
                (draft_id, accident_id, actor_id, now),
            )
            self.connection.execute(
                "INSERT INTO liability_versions(version_id,draft_id,accident_id,version_no,state,trigger_reason,"
                "change_note,content_json,content_sha256,share_changed,materials_changed,final_required,"
                "created_by,created_at) VALUES(?,?,?,1,'draft','initial',NULL,?,?,0,0,?,?,?)",
                (version_id, draft_id, accident_id, canonical_json(content), digest,
                 final_required, actor_id, now),
            )
            self._audit("liability_draft", draft_id, "draft.created", actor_id, {
                "accident_id": accident_id, "version_no": 1, "version_id": version_id,
                "final_required": bool(final_required),
            })
        return self.version_detail(actor_id, draft_id, 1)

    def edit_draft(self, actor_id: str, draft_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """覆盖尚未提交（或已退回后尚未重新提交？仅 draft 态）的草案内容，不产生新版本。"""
        self._require(actor_id, "draft.write")
        draft_row = self._draft_row(draft_id)
        version_row = self._current_version(draft_row)
        if version_row["state"] != "draft":
            raise InvalidState("只有未提交的草案可以直接修改；其他状态请使用修订形成新版本")
        content = self._build_content(draft_row["accident_id"], raw, complete=False)
        digest = self._content_digest(content)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE liability_versions SET content_json=?,content_sha256=? WHERE version_id=?",
                (canonical_json(content), digest, version_row["version_id"]),
            )
            self._audit("liability_draft", draft_id, "version.edited", actor_id, {
                "version_no": version_row["version_no"], "content_sha256": digest,
            })
        return self.version_detail(actor_id, draft_id, version_row["version_no"])

    @staticmethod
    def _diff_versions(old_content: Mapping[str, Any], new_content: Mapping[str, Any]) -> tuple[bool, bool]:
        old_shares = {item["party_id"]: item["share_percent"] for item in old_content["party_shares"]}
        new_shares = {item["party_id"]: item["share_percent"] for item in new_content["party_shares"]}
        share_changed = old_shares != new_shares
        old_materials = {
            (item["category"], item["reference"]): (item["title"], item["detail"])
            for item in old_content["materials"]
        }
        new_materials = {
            (item["category"], item["reference"]): (item["title"], item["detail"])
            for item in new_content["materials"]
        }
        materials_changed = old_materials != new_materials
        return share_changed, materials_changed

    @staticmethod
    def _basis_map(content: Mapping[str, Any]) -> dict[str, str]:
        return {item["party_id"]: item["basis"] for item in content["party_shares"]}

    def revise_draft(
        self,
        actor_id: str,
        draft_id: str,
        raw: Mapping[str, Any],
        trigger_reason: str,
        change_note: str | None = None,
    ) -> dict[str, Any]:
        """补充证据、修改责任比例或按复核意见修订：形成新版本，旧签署按规则失效。"""
        self._require(actor_id, "draft.write")
        if trigger_reason not in TRIGGER_REASONS:
            raise ValidationFailed(f"trigger_reason 必须是 {sorted(TRIGGER_REASONS)} 之一")
        note = change_note.strip() if isinstance(change_note, str) and change_note.strip() else None
        draft_row = self._draft_row(draft_id)
        accident = self._accident_row(draft_row["accident_id"])
        with transaction(self.connection, immediate=True):
            current = self._current_version(draft_row)
            if current["state"] in {"submitted", "signed"}:
                raise InvalidState("认定正在签署流程中，需先由复核环节退回才能修订")
            if current["state"] not in {"returned", "effective"}:
                raise InvalidState("当前草案状态不允许修订")
            if current["state"] == "returned" and trigger_reason != "review_returned":
                raise ValidationFailed("复核退回后的修订必须使用 trigger_reason=review_returned")
            if current["state"] == "effective" and trigger_reason == "review_returned":
                raise ValidationFailed("已生效版本的修订原因不能是 review_returned")
            old_content = json.loads(current["content_json"])
            content = self._build_content(
                draft_row["accident_id"], raw,
                complete=trigger_reason != "other",
            )
            share_changed, materials_changed = self._diff_versions(old_content, content)
            if trigger_reason == "supplement_evidence" and not materials_changed:
                raise ValidationFailed("未检测到材料变化，补充证据的修订原因不成立")
            if trigger_reason == "liability_share_changed" and not share_changed:
                raise ValidationFailed("未检测到责任比例变化，修改责任比例的修订原因不成立")
            if trigger_reason == "other" and not (
                share_changed
                or materials_changed
                or old_content.get("conclusion") != content["conclusion"]
                or self._basis_map(old_content) != self._basis_map(content)
            ):
                raise ValidationFailed("修订内容与当前版本完全相同，不能形成新版本")
            new_no = current["version_no"] + 1
            version_id = f"{draft_id}-V{new_no}"
            digest = self._content_digest(content)
            now = self._now()
            final_required = 1 if final_review_required(
                bool(accident["has_casualty"]), bool(accident["evidence_chain_anomaly"])
            ) else 0
            # 旧版本一律不再是当前版本；曾经的意见逐行保留，仅按规则置为失效。
            invalidate = share_changed or materials_changed or current["state"] == "returned"
            if current["state"] == "returned":
                reason = f"第{current['version_no']}版经复核退回，由第{new_no}版承接"
            else:
                triggers = []
                if materials_changed:
                    triggers.append("补充证据")
                if share_changed:
                    triggers.append("修改责任比例")
                reason = f"第{current['version_no']}版因{'、'.join(triggers) or '版本修订'}失效，由第{new_no}版承接"
            self.connection.execute(
                "UPDATE liability_versions SET state='superseded' WHERE version_id=?",
                (current["version_id"],),
            )
            self._audit("liability_draft", draft_id, "version.superseded", actor_id, {
                "version_no": current["version_no"], "new_version_no": new_no,
            })
            if invalidate:
                old_signings = self.connection.execute(
                    "SELECT signing_id,level FROM version_signings WHERE version_id=? AND status='valid'",
                    (current["version_id"],),
                ).fetchall()
                for signing in old_signings:
                    self.connection.execute(
                        "UPDATE version_signings SET status='invalidated',invalidated_at=?,invalidated_reason=? "
                        "WHERE signing_id=? AND status='valid'",
                        (now, reason, signing["signing_id"]),
                    )
                    self._audit("liability_draft", draft_id, "signing.invalidated", actor_id, {
                        "version_no": current["version_no"], "level": signing["level"],
                        "signing_id": signing["signing_id"], "reason": reason,
                    })
            self.connection.execute(
                "INSERT INTO liability_versions(version_id,draft_id,accident_id,version_no,state,trigger_reason,"
                "change_note,content_json,content_sha256,share_changed,materials_changed,final_required,"
                "created_by,created_at) VALUES(?,?,?,?,'draft',?,?,?,?,?,?,?,?,?)",
                (version_id, draft_id, draft_row["accident_id"], new_no, trigger_reason, note,
                 canonical_json(content), digest, 1 if share_changed else 0, 1 if materials_changed else 0,
                 final_required, actor_id, now),
            )
            self.connection.execute(
                "UPDATE liability_drafts SET current_version_no=? WHERE draft_id=?",
                (new_no, draft_id),
            )
            self._audit("liability_draft", draft_id, "version.created", actor_id, {
                "version_no": new_no, "version_id": version_id, "trigger_reason": trigger_reason,
                "change_note": note, "share_changed": share_changed,
                "materials_changed": materials_changed, "signings_invalidated": invalidate,
            })
        return self.version_detail(actor_id, draft_id, new_no)

    # ---------- 分级签署 ----------

    def _signings(self, version_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT signing_id,level,action,signer_id,opinion,content_sha256,status,signed_at,"
            "invalidated_at,invalidated_reason FROM version_signings WHERE version_id=? ORDER BY signing_id",
            (version_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _valid_signing(self, version_id: str, level: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM version_signings WHERE version_id=? AND level=? AND status='valid' AND action='signed'",
            (version_id, level),
        ).fetchone()

    @staticmethod
    def _required_levels(version_row: sqlite3.Row) -> list[str]:
        levels = ["submit", "review"]
        if version_row["final_required"]:
            levels.append("final")
        return levels

    def submit_draft(self, actor_id: str, draft_id: str, opinion: str) -> dict[str, Any]:
        actor = self._require(actor_id, "draft.submit")
        opinion = self._text(opinion, "opinion", 2000)
        draft_row = self._draft_row(draft_id)
        with transaction(self.connection, immediate=True):
            version_row = self._current_version(draft_row)
            existing = self._valid_signing(version_row["version_id"], "submit")
            if existing is not None:
                return self.version_detail(actor_id, draft_id, version_row["version_no"]) | {"already_signed": True}
            if version_row["state"] != "draft":
                raise InvalidState("只有草稿状态的当前版本可以提交签署")
            content = json.loads(version_row["content_json"])
            try:
                ensure_material_coverage(m["category"] for m in content["materials"])
            except LiabilityValidationError as exc:
                raise ValidationFailed(str(exc)) from exc
            cursor = self.connection.execute(
                "INSERT INTO version_signings(version_id,level,action,signer_id,opinion,content_sha256,"
                "status,signed_at) VALUES(?,?, 'signed',?,?,?, 'valid',?)",
                (version_row["version_id"], "submit", actor_id, opinion,
                 version_row["content_sha256"], self._now()),
            )
            self.connection.execute(
                "UPDATE liability_versions SET state='submitted',submitted_by=?,submitted_at=? WHERE version_id=?",
                (actor_id, self._now(), version_row["version_id"]),
            )
            self._audit("liability_draft", draft_id, "version.submitted", actor_id, {
                "version_no": version_row["version_no"], "signing_id": cursor.lastrowid,
            })
        result = self.version_detail(actor_id, draft_id, draft_row["current_version_no"])
        result["already_signed"] = False
        return result

    def review_draft(
        self, actor_id: str, draft_id: str, approve: bool, opinion: str
    ) -> dict[str, Any]:
        self._require(actor_id, "review.sign")
        opinion = self._text(opinion, "opinion", 2000)
        draft_row = self._draft_row(draft_id)
        with transaction(self.connection, immediate=True):
            version_row = self._current_version(draft_row)
            existing = self._valid_signing(version_row["version_id"], "review")
            if existing is not None:
                return self.version_detail(actor_id, draft_id, version_row["version_no"]) | {"already_signed": True}
            if version_row["state"] != "submitted":
                raise InvalidState("只有主办民警已提交的版本可以复核")
            if version_row["submitted_by"] == actor_id:
                raise Forbidden("复核人员不能是提交草案的主办民警本人")
            now = self._now()
            if approve:
                cursor = self.connection.execute(
                    "INSERT INTO version_signings(version_id,level,action,signer_id,opinion,content_sha256,"
                    "status,signed_at) VALUES(?,?, 'signed',?,?,?, 'valid',?)",
                    (version_row["version_id"], "review", actor_id, opinion,
                     version_row["content_sha256"], now),
                )
                if version_row["final_required"]:
                    new_state = "signed"
                else:
                    new_state = "effective"
                self.connection.execute(
                    "UPDATE liability_versions SET state=? WHERE version_id=?",
                    (new_state, version_row["version_id"]),
                )
                self._audit("liability_draft", draft_id, "version.review_signed", actor_id, {
                    "version_no": version_row["version_no"], "signing_id": cursor.lastrowid,
                    "final_required": bool(version_row["final_required"]),
                })
                if new_state == "effective":
                    self.connection.execute(
                        "UPDATE liability_versions SET effective_at=? WHERE version_id=?",
                        (now, version_row["version_id"]),
                    )
                    self._audit("liability_draft", draft_id, "version.effective", actor_id, {
                        "version_no": version_row["version_no"], "effective_at": now,
                        "reason": "复核签署后所需层级已完成",
                    })
            else:
                cursor = self.connection.execute(
                    "INSERT INTO version_signings(version_id,level,action,signer_id,opinion,content_sha256,"
                    "status,signed_at) VALUES(?,?, 'returned',?,?,?, 'valid',?)",
                    (version_row["version_id"], "review", actor_id, opinion,
                     version_row["content_sha256"], now),
                )
                self.connection.execute(
                    "UPDATE liability_versions SET state='returned' WHERE version_id=?",
                    (version_row["version_id"],),
                )
                self._audit("liability_draft", draft_id, "version.returned", actor_id, {
                    "version_no": version_row["version_no"], "signing_id": cursor.lastrowid,
                    "return_opinion": opinion,
                })
        result = self.version_detail(actor_id, draft_id, draft_row["current_version_no"])
        result["already_signed"] = False
        return result

    def final_sign(self, actor_id: str, draft_id: str, opinion: str) -> dict[str, Any]:
        self._require(actor_id, "final.sign")
        opinion = self._text(opinion, "opinion", 2000)
        draft_row = self._draft_row(draft_id)
        with transaction(self.connection, immediate=True):
            version_row = self._current_version(draft_row)
            existing = self._valid_signing(version_row["version_id"], "final")
            if existing is not None:
                return self.version_detail(actor_id, draft_id, version_row["version_no"]) | {"already_signed": True}
            if not version_row["final_required"]:
                raise InvalidState("本案不涉及人员伤亡或证据链异常，无需负责人终审")
            if version_row["state"] != "signed":
                raise InvalidState("只有复核通过、等待终审的版本可以终审")
            review = self._valid_signing(version_row["version_id"], "review")
            if review is not None and review["signer_id"] == actor_id:
                raise Forbidden("负责人不能签署本人已复核的版本")
            now = self._now()
            cursor = self.connection.execute(
                "INSERT INTO version_signings(version_id,level,action,signer_id,opinion,content_sha256,"
                "status,signed_at) VALUES(?,?, 'signed',?,?,?, 'valid',?)",
                (version_row["version_id"], "final", actor_id, opinion,
                 version_row["content_sha256"], now),
            )
            self.connection.execute(
                "UPDATE liability_versions SET state='effective',effective_at=? WHERE version_id=?",
                (now, version_row["version_id"]),
            )
            self._audit("liability_draft", draft_id, "version.final_signed", actor_id, {
                "version_no": version_row["version_no"], "signing_id": cursor.lastrowid,
            })
            self._audit("liability_draft", draft_id, "version.effective", actor_id, {
                "version_no": version_row["version_no"], "effective_at": now,
                "reason": "负责人终审后所需层级已完成",
            })
        result = self.version_detail(actor_id, draft_id, draft_row["current_version_no"])
        result["already_signed"] = False
        return result

    # ---------- 查询 ----------

    def _version_row(self, draft_id: str, version_no: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM liability_versions WHERE draft_id=? AND version_no=?",
            (draft_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFound(f"认定版本不存在: {draft_id} 第 {version_no} 版")
        return row

    def version_detail(self, actor_id: str, draft_id: str, version_no: int) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        draft_row = self._draft_row(draft_id)
        row = self._version_row(draft_id, version_no)
        return self._version_dto(row, draft_row["current_version_no"])

    def _version_dto(self, row: sqlite3.Row, current_no: int) -> dict[str, Any]:
        content = json.loads(row["content_json"])
        required_levels = self._required_levels(row)
        valid_levels = {
            item["level"]
            for item in self._signings(row["version_id"])
            if item["status"] == "valid" and item["action"] == "signed"
        }
        document = self.connection.execute(
            "SELECT document_id,document_no,content_sha256,generated_by,generated_at FROM service_documents "
            "WHERE version_id=?",
            (row["version_id"],),
        ).fetchone()
        return {
            "version_id": row["version_id"],
            "draft_id": row["draft_id"],
            "accident_id": row["accident_id"],
            "version_no": row["version_no"],
            "is_current": row["version_no"] == current_no,
            "state": row["state"],
            "trigger_reason": row["trigger_reason"],
            "change_note": row["change_note"],
            "final_required": bool(row["final_required"]),
            "required_levels": required_levels,
            "missing_levels": [level for level in required_levels if level not in valid_levels],
            "share_changed": bool(row["share_changed"]),
            "materials_changed": bool(row["materials_changed"]),
            "submitted_by": row["submitted_by"],
            "submitted_at": row["submitted_at"],
            "effective_at": row["effective_at"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "content_sha256": row["content_sha256"],
            "content": content,
            "signings": self._signings(row["version_id"]),
            "service_document": None if document is None else dict(document),
        }

    def history(self, actor_id: str, draft_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        draft_row = self._draft_row(draft_id)
        rows = self.connection.execute(
            "SELECT * FROM liability_versions WHERE draft_id=? ORDER BY version_no", (draft_id,)
        ).fetchall()
        return {
            "draft_id": draft_id,
            "accident_id": draft_row["accident_id"],
            "current_version_no": draft_row["current_version_no"],
            "versions": [self._version_dto(row, draft_row["current_version_no"]) for row in rows],
        }

    def _timeline(self, draft_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type='liability_draft' AND entity_id=? ORDER BY event_id",
            (draft_id,),
        ).fetchall()
        return [
            {
                "at": row["created_at"],
                "event_type": row["event_type"],
                "label": EVENT_LABELS.get(row["event_type"], row["event_type"]),
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def explanation(self, actor_id: str, draft_id: str) -> dict[str, Any]:
        """解释每一方责任、引用材料与状态变化的完整查询。"""
        self._require(actor_id, "report.read")
        draft_row = self._draft_row(draft_id)
        accident = self._accident_row(draft_row["accident_id"])
        current = self._current_version(draft_row)
        dto = self._version_dto(current, draft_row["current_version_no"])
        content = dto["content"]
        shares = {item["party_id"]: item for item in content["party_shares"]}
        all_shares = [Decimal(item["share_percent"]) for item in content["party_shares"]]
        parties = []
        for party in content["parties"]:
            item = shares[party["party_id"]]
            level = liability_level(Decimal(item["share_percent"]), all_shares)
            parties.append({
                "party_id": party["party_id"],
                "name": party["name"],
                "party_kind": party["party_kind"],
                "party_kind_label": PARTY_KINDS[party["party_kind"]],
                "share_percent": item["share_percent"],
                "liability_level": level,
                "liability_level_label": LEVEL_LABELS[level],
                "basis": item["basis"],
            })
        materials_by_category: dict[str, list[dict[str, Any]]] = {
            key: [] for key in MATERIAL_CATEGORIES
        }
        for material in content["materials"]:
            materials_by_category[material["category"]].append({
                "material_key": f"{material['category']}:{material['reference']}",
                "reference": material["reference"],
                "title": material["title"],
                "detail": material["detail"],
            })
        materials_view = {
            key: {"label": MATERIAL_CATEGORIES[key], "items": materials_by_category[key]}
            for key in MATERIAL_CATEGORIES
        }
        signings = []
        for signing in dto["signings"]:
            signings.append({
                "level": signing["level"],
                "action": signing["action"],
                "signer_id": signing["signer_id"],
                "opinion": signing["opinion"],
                "status": signing["status"],
                "signed_at": signing["signed_at"],
                "invalidated_at": signing["invalidated_at"],
                "invalidated_reason": signing["invalidated_reason"],
            })
        return {
            "draft_id": draft_id,
            "accident": {
                "accident_id": accident["accident_id"],
                "occurred_at": accident["occurred_at"],
                "location": accident["location"],
                "summary": accident["summary"],
                "has_casualty": bool(accident["has_casualty"]),
                "evidence_chain_anomaly": bool(accident["evidence_chain_anomaly"]),
            },
            "current_version_no": current["version_no"],
            "state": current["state"],
            "effective": current["state"] == "effective",
            "effective_at": current["effective_at"],
            "final_required": bool(current["final_required"]),
            "required_levels": dto["required_levels"],
            "missing_levels": dto["missing_levels"],
            "conclusion": content["conclusion"],
            "party_liabilities": parties,
            "cited_materials": materials_view,
            "signings": signings,
            "service_document": dto["service_document"],
            "status_timeline": self._timeline(draft_id),
        }

    # ---------- 送达文本 ----------

    def service_document(self, actor_id: str, draft_id: str) -> dict[str, Any]:
        """只有当前生效版本可以生成；重复获取不产生第二份决定。"""
        self._require(actor_id, "report.read")
        draft_row = self._draft_row(draft_id)
        with transaction(self.connection, immediate=True):
            version_row = self._current_version(draft_row)
            if version_row["state"] != "effective":
                raise InvalidState("只有当前版本完成所需签署层级并生效后，才能生成送达文本")
            existing = self.connection.execute(
                "SELECT * FROM service_documents WHERE version_id=?",
                (version_row["version_id"],),
            ).fetchone()
            if existing is not None:
                return {
                    "document_id": existing["document_id"],
                    "document_no": existing["document_no"],
                    "version_id": existing["version_id"],
                    "version_no": version_row["version_no"],
                    "document_text": existing["document_text"],
                    "content_sha256": existing["content_sha256"],
                    "generated_at": existing["generated_at"],
                    "already_generated": True,
                }
            content = json.loads(version_row["content_json"])
            document_no = f"LDR-{draft_row['accident_id']}-V{version_row['version_no']:02d}"
            accident = self._accident_row(draft_row["accident_id"])
            text = render_service_document(
                document_no=document_no,
                accident={
                    "accident_id": accident["accident_id"],
                    "occurred_at": accident["occurred_at"],
                    "location": accident["location"],
                },
                version_no=version_row["version_no"],
                conclusion=content["conclusion"],
                parties=content["parties"],
                shares={item["party_id"]: {
                    "share_percent": Decimal(item["share_percent"]), "basis": item["basis"],
                } for item in content["party_shares"]},
                materials=[{"material_id": m["reference"], **m} for m in content["materials"]],
                effective_at=version_row["effective_at"],
            )
            digest = content_digest([text])
            document_id = f"DOC-{version_row['version_id']}"
            now = self._now()
            try:
                self.connection.execute(
                    "INSERT INTO service_documents(document_id,version_id,draft_id,accident_id,document_no,"
                    "document_text,content_sha256,generated_by,generated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (document_id, version_row["version_id"], draft_id, draft_row["accident_id"],
                     document_no, text, digest, actor_id, now),
                )
            except sqlite3.IntegrityError:
                # 并发下另一请求已生成：回读同一份决定，不产生第二份。
                existing = self.connection.execute(
                    "SELECT * FROM service_documents WHERE version_id=?",
                    (version_row["version_id"],),
                ).fetchone()
                return {
                    "document_id": existing["document_id"],
                    "document_no": existing["document_no"],
                    "version_id": existing["version_id"],
                    "version_no": version_row["version_no"],
                    "document_text": existing["document_text"],
                    "content_sha256": existing["content_sha256"],
                    "generated_at": existing["generated_at"],
                    "already_generated": True,
                }
            self._audit("liability_draft", draft_id, "document.generated", actor_id, {
                "version_no": version_row["version_no"], "document_no": document_no,
                "document_id": document_id,
            })
        return {
            "document_id": document_id,
            "document_no": document_no,
            "version_id": version_row["version_id"],
            "version_no": version_row["version_no"],
            "document_text": text,
            "content_sha256": digest,
            "generated_at": now,
            "already_generated": False,
        }
