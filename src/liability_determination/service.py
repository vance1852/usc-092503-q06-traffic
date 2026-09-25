"""责任认定草案、版本历史、多级签署与送达文本的领域用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from .clock import SystemClock, isoformat
from . import contracts
from .contracts import ValidationError
from .document import build_document_text, canonical_json, document_number
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    # 主办民警：登记案件材料、起草并提交认定
    "investigator": {"case.write", "material.write", "draft.write", "draft.submit", "report.read"},
    # 复核人员：复核签署
    "reviewer": {"signature.review", "report.read"},
    # 负责人：终审签署（人员伤亡或证据链异常时）
    "chief": {"signature.final", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

LEVEL_BY_PERMISSION = {"signature.review": "review", "signature.final": "final"}
LEVEL_ORDER = {"review": 0, "final": 1}


class LiabilityDeterminationService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    # ----- 基础辅助 -----

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id,display_name,role,active FROM users WHERE user_id=?", (user_id,)
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

    def _case(self, case_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFound("案件不存在")
        return row

    def _draft(self, case_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM determination_drafts WHERE case_id=?", (case_id,)
        ).fetchone()
        if row is None:
            raise NotFound("责任认定草案不存在")
        return row

    def _version(self, version_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM determination_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound("认定版本不存在")
        return row

    def _current_version(self, draft_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM determination_versions WHERE draft_id=? AND is_current=1", (draft_id,)
        ).fetchone()
        if row is None:
            raise InvalidState("草案没有当前版本")
        return row

    # ----- 用户与基础资料 -----

    def create_user(self, actor_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not actor_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (actor_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {actor_id}") from exc
        return {"user_id": actor_id.strip(), "role": role}

    def create_case(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "case.write")
        try:
            data = contracts.parse_case(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO cases(case_id,case_number,title,occurred_at,location,casualties,evidence_anomaly,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        data["case_id"], data["case_number"], data["title"], data["occurred_at"],
                        data["location"], int(data["casualties"]), int(data["evidence_anomaly"]),
                        actor_id, self._now(),
                    ),
                )
                self._audit("case", data["case_id"], "case.created", actor_id, {
                    "case_id": data["case_id"],
                    "casualties": data["casualties"],
                    "evidence_anomaly": data["evidence_anomaly"],
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("案件编号冲突") from exc
        return self.get_case(data["case_id"])

    def get_case(self, case_id: str) -> dict[str, Any]:
        row = self._case(case_id)
        return self._case_view(row)

    @staticmethod
    def _case_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "case_id": row["case_id"],
            "case_number": row["case_number"],
            "title": row["title"],
            "occurred_at": row["occurred_at"],
            "location": row["location"],
            "casualties": bool(row["casualties"]),
            "evidence_anomaly": bool(row["evidence_anomaly"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def add_party(self, actor_id: str, case_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "case.write")
        self._case(case_id)
        try:
            data = contracts.parse_party(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        draft = self.connection.execute(
            "SELECT draft_id FROM determination_drafts WHERE case_id=?", (case_id,)
        ).fetchone()
        if draft is not None:
            raise InvalidState("草案已建立，不能再增补当事方；如需变更请新建案件")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO parties(party_id,case_id,name,kind,contact,created_at) VALUES(?,?,?,?,?,?)",
                    (data["party_id"], case_id, data["name"], data["kind"], data["contact"], self._now()),
                )
                self._audit("case", case_id, "party.added", actor_id, {
                    "case_id": case_id, "party_id": data["party_id"],
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("当事方编号冲突") from exc
        return {"case_id": case_id, **data}

    def add_material(self, actor_id: str, case_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "material.write")
        case = self._case(case_id)
        try:
            data = contracts.parse_material(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        # 已提交或已生效的版本不受影响：版本只引用建版时固定的材料集合；
        # 新材料只能在主办修订形成的新版本中被引用。
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO evidence_materials(material_id,case_id,material_type,title,summary,source_ref,"
                    "recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        data["material_id"], case_id, data["material_type"], data["title"], data["summary"],
                        data["source_ref"], actor_id, self._now(),
                    ),
                )
                self._audit("case", case_id, "material.added", actor_id, {
                    "case_id": case_id, "material_id": data["material_id"],
                    "material_type": data["material_type"],
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("证据材料编号冲突") from exc
        return {
            "case_id": case_id, **data,
            "recorded_by": actor_id, "recorded_at": self._now(),
        }

    # ----- 草案与版本内容 -----

    @staticmethod
    def _content_hash(content: Mapping[str, Any]) -> str:
        return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()

    def _validate_content(self, case_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        try:
            content = contracts.parse_version_content(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        party_rows = self.connection.execute(
            "SELECT party_id FROM parties WHERE case_id=?", (case_id,)
        ).fetchall()
        known_parties = {row["party_id"] for row in party_rows}
        used_parties = {item["party_id"] for item in content["party_findings"]}
        missing = used_parties - known_parties
        if missing:
            raise ValidationFailed(f"当事方不属于该案件: {sorted(missing)}")
        if used_parties != known_parties:
            raise ValidationFailed("认定版本必须覆盖案件的全部当事方")
        material_rows = self.connection.execute(
            "SELECT material_id,material_type FROM evidence_materials WHERE case_id=?", (case_id,)
        ).fetchall()
        known_materials = {row["material_id"]: row["material_type"] for row in material_rows}
        used_materials = {item["material_id"] for item in content["material_citations"]}
        missing_materials = used_materials - set(known_materials)
        if missing_materials:
            raise ValidationFailed(f"证据材料不属于该案件: {sorted(missing_materials)}")
        if not used_materials:
            raise ValidationFailed("认定版本至少要引用一份证据材料")
        used_types = {known_materials[mid] for mid in used_materials}
        missing_types = set(contracts.MATERIAL_TYPES) - used_types
        if missing_types:
            labels = [contracts.MATERIAL_TYPE_LABELS[item] for item in sorted(missing_types)]
            raise ValidationFailed(f"认定版本缺少必须汇总的材料类别: {'、'.join(labels)}")
        return content

    def _insert_version_content(self, version_id: int, content: Mapping[str, Any]) -> None:
        for item in content["party_findings"]:
            self.connection.execute(
                "INSERT INTO version_parties(version_id,party_id,responsibility_ratio,finding,reasoning) "
                "VALUES(?,?,?,?,?)",
                (version_id, item["party_id"], item["responsibility_ratio"], item["finding"], item["reasoning"]),
            )
        for item in content["material_citations"]:
            material_type = self.connection.execute(
                "SELECT material_type FROM evidence_materials WHERE material_id=?", (item["material_id"],)
            ).fetchone()["material_type"]
            self.connection.execute(
                "INSERT INTO version_materials(version_id,material_id,material_type,cited_note) VALUES(?,?,?,?)",
                (version_id, item["material_id"], material_type, item["cited_note"]),
            )

    def create_draft(self, actor_id: str, case_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "draft.write")
        case = self._case(case_id)
        if self.connection.execute(
            "SELECT 1 FROM determination_drafts WHERE case_id=?", (case_id,)
        ).fetchone():
            raise Conflict("该案件已经建立责任认定草案")
        content = self._validate_content(case_id, raw)
        now = self._now()
        content_hash = self._content_hash(content)
        with transaction(self.connection, immediate=True):
            draft_id = f"draft-{case_id}"
            self.connection.execute(
                "INSERT INTO determination_drafts(draft_id,case_id,created_by,created_at) VALUES(?,?,?,?)",
                (draft_id, case_id, actor_id, now),
            )
            cursor = self.connection.execute(
                "INSERT INTO determination_versions(draft_id,version_no,status,basis_note,content_sha256,"
                "is_current,change_summary,created_by,created_at) VALUES(?,1,'draft',?,? ,1,NULL,?,?)",
                (draft_id, content["basis_note"], content_hash, actor_id, now),
            )
            version_id = int(cursor.lastrowid)
            self._insert_version_content(version_id, content)
            self._audit("case", case_id, "draft.created", actor_id, {
                "case_id": case_id, "draft_id": draft_id, "version_no": 1, "content_sha256": content_hash,
            })
        return self.get_determination(actor_id, case_id)

    def save_draft(self, actor_id: str, case_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """覆盖当前草稿版本的内容；仅允许在尚未提交的草稿上进行。"""

        self._require(actor_id, "draft.write")
        draft = self._draft(case_id)
        version = self._current_version(draft["draft_id"])
        if version["created_by"] != actor_id:
            raise Forbidden("只有主办民警可以修改自己的草案")
        content = self._validate_content(case_id, raw)
        content_hash = self._content_hash(content)
        with transaction(self.connection, immediate=True):
            # 权威状态检查在写锁内：并发提交后此处不再可改
            cursor = self.connection.execute(
                "UPDATE determination_versions SET basis_note=?,content_sha256=? "
                "WHERE version_id=? AND status='draft' AND is_current=1",
                (content["basis_note"], content_hash, version["version_id"]),
            )
            if cursor.rowcount != 1:
                raise InvalidState("当前版本不是可编辑草稿，变更内容请发起修订生成新版本")
            self.connection.execute(
                "DELETE FROM version_parties WHERE version_id=?", (version["version_id"],)
            )
            self.connection.execute(
                "DELETE FROM version_materials WHERE version_id=?", (version["version_id"],)
            )
            self._insert_version_content(version["version_id"], content)
            self._audit("case", case_id, "draft.saved", actor_id, {
                "case_id": case_id, "version_no": version["version_no"], "content_sha256": content_hash,
            })
        return self.get_determination(actor_id, case_id)

    def submit_draft(self, actor_id: str, case_id: str, raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """主办民警提交当前版本，进入复核签署流程。"""

        self._require(actor_id, "draft.submit")
        draft = self._draft(case_id)
        version = self._current_version(draft["draft_id"])
        if version["created_by"] != actor_id:
            raise Forbidden("只有主办民警可以提交认定草案")
        if raw is not None:
            content = self._validate_content(case_id, raw)
            content_hash = self._content_hash(content)
        else:
            content = None
            content_hash = version["content_sha256"]
        with transaction(self.connection, immediate=True):
            # 权威状态转换在写锁内，保证只能提交一次
            cursor = self.connection.execute(
                "UPDATE determination_versions SET status='submitted',basis_note=?,content_sha256=?,"
                "submitted_at=? WHERE version_id=? AND status='draft' AND is_current=1",
                (
                    content["basis_note"] if content is not None else version["basis_note"],
                    content_hash, self._now(), version["version_id"],
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidState("当前版本已经提交，不能重复提交")
            if content is not None:
                self.connection.execute(
                    "DELETE FROM version_parties WHERE version_id=?", (version["version_id"],)
                )
                self.connection.execute(
                    "DELETE FROM version_materials WHERE version_id=?", (version["version_id"],)
                )
                self._insert_version_content(version["version_id"], content)
            self._audit("case", case_id, "version.submitted", actor_id, {
                "case_id": case_id, "version_no": version["version_no"], "content_sha256": content_hash,
            })
        return self.get_determination(actor_id, case_id)

    def revise_draft(
        self, actor_id: str, case_id: str, raw: Mapping[str, Any], change_summary: str
    ) -> dict[str, Any]:
        """补充证据或修改责任比例等修订：旧版本及其签署、送达文本原样保留，
        旧签署标记失效，产生新的草稿版本。"""

        self._require(actor_id, "draft.write")
        if not isinstance(change_summary, str) or not change_summary.strip():
            raise ValidationFailed("修订必须填写 change_summary 说明修订原因")
        draft = self._draft(case_id)
        version = self._current_version(draft["draft_id"])
        if version["created_by"] != actor_id:
            raise Forbidden("只有主办民警可以修订认定草案")
        if version["status"] == "draft":
            raise InvalidState("当前版本尚未提交，请直接保存草稿而非修订")
        if version["status"] == "submitted":
            raise InvalidState("当前版本仍在签署流程中，被驳回或完成终审后才能修订")
        content = self._validate_content(case_id, raw)
        content_hash = self._content_hash(content)
        with transaction(self.connection, immediate=True):
            # 写锁内重读，权威判定“当前版本”与可修订状态
            locked = self.connection.execute(
                "SELECT * FROM determination_versions WHERE version_id=? AND is_current=1",
                (version["version_id"],),
            ).fetchone()
            if locked is None or locked["status"] not in {"rejected", "effective"}:
                raise InvalidState("当前版本仍在签署流程中或已被修订，不能据此发起修订")
            now = self._now()
            # 旧版本退出当前版本；曾生效的版本整体状态转为 superseded，被驳回的保留 rejected
            self.connection.execute(
                "UPDATE determination_versions SET is_current=0 WHERE version_id=?",
                (locked["version_id"],),
            )
            self.connection.execute(
                "UPDATE determination_versions SET status='superseded' "
                "WHERE version_id=? AND status='effective'",
                (locked["version_id"],),
            )
            cursor = self.connection.execute(
                "UPDATE version_signatures SET status='superseded',superseded_at=? "
                "WHERE version_id=? AND status='active'",
                (now, locked["version_id"]),
            )
            superseded_signatures = cursor.rowcount
            old_effect = self.connection.execute(
                "SELECT effect_id FROM determination_effects WHERE version_id=? AND superseded_at IS NULL",
                (locked["version_id"],),
            ).fetchone()
            if old_effect is not None:
                self.connection.execute(
                    "UPDATE determination_effects SET superseded_at=? WHERE effect_id=?",
                    (now, old_effect["effect_id"]),
                )
            new_no = locked["version_no"] + 1
            try:
                new_cursor = self.connection.execute(
                    "INSERT INTO determination_versions(draft_id,version_no,status,basis_note,content_sha256,"
                    "is_current,change_summary,created_by,created_at) VALUES(?,?,'draft',?,? ,1,?,?,?)",
                    (draft["draft_id"], new_no, content["basis_note"], content_hash,
                     change_summary.strip(), actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("并发修订导致版本冲突，请重试") from exc
            new_version_id = int(new_cursor.lastrowid)
            self._insert_version_content(new_version_id, content)
            self._audit("case", case_id, "version.revised", actor_id, {
                "case_id": case_id,
                "from_version_no": locked["version_no"],
                "to_version_no": new_no,
                "change_summary": change_summary.strip(),
                "superseded_signatures": superseded_signatures,
                "prior_effect_superseded": old_effect is not None,
                "content_sha256": content_hash,
            })
        return self.get_determination(actor_id, case_id)

    # ----- 签署与生效 -----

    @staticmethod
    def _required_levels(case: sqlite3.Row) -> list[str]:
        levels = ["review"]
        if case["casualties"] or case["evidence_anomaly"]:
            levels.append("final")
        return levels

    def _active_signatures(self, version_id: int) -> dict[str, sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT * FROM version_signatures WHERE version_id=? AND status='active'",
            (version_id,),
        ).fetchall()
        return {row["level"]: row for row in rows}

    def sign(
        self, actor_id: str, case_id: str, level: str, opinion: str, comment: str = ""
    ) -> dict[str, Any]:
        if level == "review":
            user = self._require(actor_id, "signature.review")
        elif level == "final":
            user = self._require(actor_id, "signature.final")
        else:
            raise ValidationFailed("签署层级必须是 review 或 final")
        if opinion not in {"agree", "reject"}:
            raise ValidationFailed("签署意见必须是 agree 或 reject")
        case = self._case(case_id)
        draft = self._draft(case_id)
        required = self._required_levels(case)
        if level not in required:
            raise InvalidState("该案件不需要负责人终审" if level == "final" else "该签署层级不适用")
        current = self._current_version(draft["draft_id"])
        if current["created_by"] == actor_id:
            raise Forbidden("主办民警不能签署自己提交的认定版本")
        with transaction(self.connection, immediate=True):
            # 写锁内重读，防止与提交/修订并发
            version = self.connection.execute(
                "SELECT * FROM determination_versions WHERE version_id=? AND is_current=1",
                (current["version_id"],),
            ).fetchone()
            if version is None:
                raise InvalidState("该版本已被修订，不再是当前版本")
            existing = self.connection.execute(
                "SELECT * FROM version_signatures WHERE version_id=? AND level=? AND status='active'",
                (version["version_id"], level),
            ).fetchone()
            # 重复签署幂等：该层级已有生效签署时，不产生第二条签署、不生成第二份决定。
            # 检查在写锁内且先于状态判断，版本驳回或生效后的重复提交同样安全。
            if existing is not None:
                if existing["signer_id"] != actor_id:
                    raise Conflict("该层级已经完成签署")
                if existing["opinion"] != opinion:
                    raise Conflict("该层级意见已经形成且不可更改，调整结论请由主办修订形成新版本")
                return self.get_determination(actor_id, case_id)
            if version["status"] != "submitted":
                raise InvalidState("只有已提交的当前版本可以签署")
            active = self._active_signatures(version["version_id"])
            required_before = [item for item in required if LEVEL_ORDER[item] < LEVEL_ORDER[level]]
            for item in required_before:
                if item not in active or active[item]["opinion"] != "agree":
                    raise InvalidState(f"必须先完成前置层级签署: {item}")
            now = self._now()
            try:
                self.connection.execute(
                    "INSERT INTO version_signatures(version_id,level,status,opinion,comment,signer_id,signed_at) "
                    "VALUES(?,?,'active',?,?,?,?)",
                    (version["version_id"], level, opinion, comment.strip(), actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该层级已经完成签署") from exc
            self._audit("case", case_id, "version.signed", actor_id, {
                "case_id": case_id, "version_no": version["version_no"],
                "level": level, "opinion": opinion,
            })
            if opinion == "reject":
                self.connection.execute(
                    "UPDATE determination_versions SET status='rejected' WHERE version_id=?",
                    (version["version_id"],),
                )
                self._audit("case", case_id, "version.rejected", actor_id, {
                    "case_id": case_id, "version_no": version["version_no"], "level": level,
                })
            else:
                effect = self._maybe_effective(case, draft, version, now)
                if effect is not None:
                    self._audit("case", case_id, "determination.effective", actor_id, {
                        "case_id": case_id, "version_no": version["version_no"],
                        "document_no": effect["document_no"],
                    })
        return self.get_determination(actor_id, case_id)

    def _maybe_effective(
        self, case: sqlite3.Row, draft: sqlite3.Row, version: sqlite3.Row, now: str
    ) -> dict[str, Any] | None:
        """在事务内检查所需层级是否全部同意；满足则让版本生效并生成唯一送达文本。"""

        active = self._active_signatures(version["version_id"])
        required = self._required_levels(case)
        if not all(active.get(item) is not None and active[item]["opinion"] == "agree" for item in required):
            return None
        existing = self.connection.execute(
            "SELECT * FROM determination_effects WHERE version_id=?", (version["version_id"],)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        party_rows = self.connection.execute(
            "SELECT p.party_id,p.name,p.kind,vp.responsibility_ratio,vp.finding,vp.reasoning "
            "FROM version_parties vp JOIN parties p ON p.party_id=vp.party_id AND p.case_id=? "
            "WHERE vp.version_id=? ORDER BY p.party_id",
            (case["case_id"], version["version_id"]),
        ).fetchall()
        material_rows = self.connection.execute(
            "SELECT m.material_id,m.material_type,m.title,m.summary,m.source_ref,vm.cited_note "
            "FROM version_materials vm JOIN evidence_materials m "
            "ON m.material_id=vm.material_id AND m.case_id=? WHERE vm.version_id=? ORDER BY m.material_id",
            (case["case_id"], version["version_id"]),
        ).fetchall()
        signature_rows = self.connection.execute(
            "SELECT level,opinion,comment,signer_id,signed_at FROM version_signatures "
            "WHERE version_id=? AND status='active' ORDER BY CASE level WHEN 'review' THEN 0 ELSE 1 END",
            (version["version_id"],),
        ).fetchall()
        doc_no = document_number(case["case_id"], version["version_no"], version["content_sha256"])
        text = build_document_text(
            case=self._case_view(case),
            version_no=version["version_no"],
            basis_note=version["basis_note"],
            party_findings=[dict(row) for row in party_rows],
            materials=[dict(row) for row in material_rows],
            signatures=[dict(row) for row in signature_rows],
            effective_at=now,
        )
        cursor = self.connection.execute(
            "INSERT INTO determination_effects(version_id,draft_id,case_id,document_no,document_text,"
            "effective_at) VALUES(?,?,?,?,?,?)",
            (version["version_id"], draft["draft_id"], case["case_id"], doc_no, text, now),
        )
        self.connection.execute(
            "UPDATE determination_versions SET status='effective' WHERE version_id=?",
            (version["version_id"],),
        )
        return {
            "effect_id": cursor.lastrowid,
            "document_no": doc_no,
            "effective_at": now,
        }

    # ----- 查询 -----

    def get_determination(self, actor_id: str, case_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        case = self._case(case_id)
        draft = self._draft(case_id)
        current = self._current_version(draft["draft_id"])
        return self._determination_view(case, draft, current, include_history=True)

    def get_version(self, actor_id: str, case_id: str, version_no: int) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        case = self._case(case_id)
        draft = self._draft(case_id)
        row = self.connection.execute(
            "SELECT * FROM determination_versions WHERE draft_id=? AND version_no=?",
            (draft["draft_id"], version_no),
        ).fetchone()
        if row is None:
            raise NotFound("认定版本不存在")
        return self._determination_view(case, draft, row, include_history=True)

    def _signatures_view(self, version_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT level,status,opinion,comment,signer_id,signed_at,superseded_at "
            "FROM version_signatures WHERE version_id=? ORDER BY signature_id",
            (version_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _version_view(self, case: sqlite3.Row, row: sqlite3.Row) -> dict[str, Any]:
        required = self._required_levels(case)
        signatures = self._signatures_view(row["version_id"])
        # 当前版本只统计生效签署；历史版本（已被修订）的签署均为 superseded，
        # 但仍要展示该版本当时各层级的意见，而不是误报为 pending。
        preferred: dict[str, dict[str, Any]] = {}
        for item in signatures:
            old = preferred.get(item["level"])
            if old is None or (old["status"] == "superseded" and item["status"] == "active"):
                preferred[item["level"]] = item
        level_status = {}
        for level in required:
            item = preferred.get(level)
            level_status[level] = "pending" if item is None else item["opinion"]
        effect = self.connection.execute(
            "SELECT document_no,effective_at,superseded_at FROM determination_effects WHERE version_id=?",
            (row["version_id"],),
        ).fetchone()
        return {
            "version_id": row["version_id"],
            "version_no": row["version_no"],
            "status": row["status"],
            "is_current": bool(row["is_current"]),
            "basis_note": row["basis_note"],
            "content_sha256": row["content_sha256"],
            "change_summary": row["change_summary"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "submitted_at": row["submitted_at"],
            "required_levels": required,
            "level_status": level_status,
            "pending_levels": [
                level for level in required
                if level_status[level] == "pending" and bool(row["is_current"])
            ],
            "signatures": signatures,
            "effective": effect is not None,
            "effect": None if effect is None else dict(effect),
        }

    def _parties_view(self, case_id: str, version_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT p.party_id,p.name,p.kind,p.contact,vp.responsibility_ratio,vp.finding,vp.reasoning "
            "FROM parties p LEFT JOIN version_parties vp "
            "ON vp.party_id=p.party_id AND vp.version_id=? WHERE p.case_id=? ORDER BY p.party_id",
            (version_id, case_id),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["included"] = item["responsibility_ratio"] is not None
            result.append(item)
        return result

    def _materials_view(self, case_id: str, version_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT m.material_id,m.material_type,m.title,m.summary,m.source_ref,m.recorded_by,m.recorded_at,"
            "vm.cited_note FROM evidence_materials m LEFT JOIN version_materials vm "
            "ON vm.material_id=m.material_id AND vm.version_id=? WHERE m.case_id=? ORDER BY m.material_id",
            (version_id, case_id),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["cited"] = item["cited_note"] is not None
            result.append(item)
        return result

    def _document_view(self, version_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT document_no,document_text,effective_at,superseded_at "
            "FROM determination_effects WHERE version_id=?",
            (version_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def _timeline(self, draft_id: str) -> list[dict[str, Any]]:
        """按业务发生顺序推导状态变化时间线（不依赖时间戳排序，
        同一时刻发生的签署与生效仍保持先签署后生效的顺序）。"""

        events: list[dict[str, Any]] = []
        versions = self.connection.execute(
            "SELECT * FROM determination_versions WHERE draft_id=? ORDER BY version_no", (draft_id,)
        ).fetchall()
        for version in versions:
            events.append({
                "at": version["created_at"], "version_no": version["version_no"],
                "type": "version_created", "status": "draft",
                "change_summary": version["change_summary"], "actor_id": version["created_by"],
            })
            if version["submitted_at"]:
                events.append({
                    "at": version["submitted_at"], "version_no": version["version_no"],
                    "type": "submitted", "actor_id": version["created_by"],
                })
            signatures = self.connection.execute(
                "SELECT * FROM version_signatures WHERE version_id=? ORDER BY signature_id",
                (version["version_id"],),
            ).fetchall()
            for signature in signatures:
                events.append({
                    "at": signature["signed_at"], "version_no": version["version_no"],
                    "type": "signed", "level": signature["level"], "opinion": signature["opinion"],
                    "comment": signature["comment"], "signer_id": signature["signer_id"],
                    "status": signature["status"], "superseded_at": signature["superseded_at"],
                })
                if signature["opinion"] == "reject" and signature["status"] == "active":
                    events.append({
                        "at": signature["signed_at"], "version_no": version["version_no"],
                        "type": "rejected", "level": signature["level"],
                    })
            effect = self.connection.execute(
                "SELECT * FROM determination_effects WHERE version_id=?",
                (version["version_id"],),
            ).fetchone()
            if effect is not None:
                events.append({
                    "at": effect["effective_at"], "version_no": version["version_no"],
                    "type": "effective", "document_no": effect["document_no"],
                })
                if effect["superseded_at"]:
                    events.append({
                        "at": effect["superseded_at"], "version_no": version["version_no"],
                        "type": "document_superseded", "document_no": effect["document_no"],
                    })
        return events

    def _determination_view(
        self, case: sqlite3.Row, draft: sqlite3.Row, version: sqlite3.Row, *, include_history: bool
    ) -> dict[str, Any]:
        view = {
            "case": self._case_view(case),
            "draft_id": draft["draft_id"],
            "created_by": draft["created_by"],
            "created_at": draft["created_at"],
            "required_levels": self._required_levels(case),
            "current_version_no": self._current_version(draft["draft_id"])["version_no"],
            "version": self._version_view(case, version),
            "parties": self._parties_view(case["case_id"], version["version_id"]),
            "materials": self._materials_view(case["case_id"], version["version_id"]),
            "document": self._document_view(version["version_id"]),
        }
        if include_history:
            history_rows = self.connection.execute(
                "SELECT * FROM determination_versions WHERE draft_id=? ORDER BY version_no",
                (draft["draft_id"],),
            ).fetchall()
            view["version_history"] = [self._version_view(case, row) for row in history_rows]
            view["timeline"] = self._timeline(draft["draft_id"])
        return view

    def audit_events(self, actor_id: str, case_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT event_id,entity_type,event_type,actor_id,payload_json,created_at "
            "FROM audit_events WHERE entity_id=? ORDER BY event_id",
            (case_id,),
        ).fetchall()
        return [
            dict(row) | {"payload": json.loads(row["payload_json"])}
            for row in rows
        ]
