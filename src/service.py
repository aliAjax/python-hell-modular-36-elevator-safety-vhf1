import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if kind == "alarm":
            self._apply_equipment_safety_effect(payload.get("equipment_id"), actor)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _safety_effect(self, equipment, equipment_id):
        alarms = [
            alarm
            for alarm in self._lookup("alarm", "*", None)
            if alarm["data"].get("equipment_id") == equipment_id
        ]
        alarm_by_id = {alarm["id"]: alarm for alarm in alarms}
        active_alarms = [alarm for alarm in alarms if alarm["status"] in self.rules.ACTIVE_ALARM_STATUSES]
        active_alarms.sort(key=lambda item: item["created_at"] + item["id"])
        active_jobs = [
            job for job in self._lookup("rescue_job", "*", None)
            if job["status"] in self.rules.ACTIVE_RESCUE_STATUSES
            and job["data"].get("alarm_id") in alarm_by_id
        ]
        active_jobs.sort(key=lambda item: item["created_at"] + item["id"])

        data = dict(equipment["data"])
        if active_alarms:
            alarm = active_alarms[0]
            if equipment["status"] not in ("suspended", "out_of_service"):
                data["pre_lock_status"] = equipment["status"]
            elif not data.get("pre_lock_status"):
                data["pre_lock_status"] = None
            data["locked_by_alarm_id"] = alarm["id"]
            return "locked", "suspended", data, alarm

        if active_jobs:
            job = active_jobs[0]
            alarm = alarm_by_id[job["data"].get("alarm_id")]
            data["locked_by_alarm_id"] = alarm["id"]
            if alarm["status"] == "false_alarm":
                data["false_alarm_lock_reason"] = "rescue_job"
            return "locked", "suspended", data, alarm

        data["locked_by_alarm_id"] = None
        data.pop("false_alarm_lock_reason", None)
        closed_alarms = [alarm for alarm in alarms if alarm["status"] == "closed"]
        false_alarms = [alarm for alarm in alarms if alarm["status"] == "false_alarm"]
        terminal = closed_alarms + false_alarms
        if terminal:
            latest = sorted(
                terminal,
                key=lambda item: (item["data"].get("released_at") or item["updated_at"], item["id"]),
            )[-1]
            if latest["status"] == "closed":
                if not data.get("recovery_required_alarm_id"):
                    data["recovery_required_alarm_id"] = latest["id"]
                return "recovery_required", "suspended", data, latest
            if latest["status"] == "false_alarm" and not data.get("recovery_required_alarm_id"):
                previous = data.pop("pre_lock_status", None)
                return "released", previous or "in_service", data, latest
            return "recovery_required", "suspended", data, latest
        return "unchanged", equipment["status"], data, None

    def _revoke_permits_for_lock(self, equipment_id, alarm, actor):
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        permits = [
            permit
            for permit in self._lookup("permit", "*", None)
            if permit["data"].get("equipment_id") == equipment_id
            and permit["status"] in ("granted", "pending_review")
        ]
        for permit in permits:
            data = dict(permit["data"])
            data.update({
                "revoked_by": actor.user_id,
                "revoked_at": now,
                "revocation_reason": "safety lock from alarm " + alarm["id"],
                "blocked_by_alarm_id": alarm["id"],
            })
            updated = self.repository.update_entity(permit["id"], permit["version"], "revoked", data)
            self.audit.record(
                permit["id"], actor, "safety_lock_revoke",
                permit["status"], updated["status"], {"alarm_id": alarm["id"]},
            )

    def _apply_equipment_safety_effect(self, equipment_id, actor):
        equipment = self.repository.get_entity(equipment_id)
        if not equipment:
            return None
        effect, next_status, next_data, alarm = self._safety_effect(equipment, equipment_id)
        if effect == "unchanged":
            return equipment
        if equipment["status"] != next_status or equipment["data"] != next_data:
            updated = self.repository.update_entity(
                equipment_id, equipment["version"], next_status, next_data
            )
            self.audit.record(
                equipment_id, actor, "safety_lock_" + effect,
                equipment["status"], next_status, {"alarm_id": alarm["id"] if alarm else None},
            )
        else:
            updated = equipment
        if effect == "locked" and alarm and alarm["status"] in self.rules.ACTIVE_ALARM_STATUSES:
            self._revoke_permits_for_lock(equipment_id, alarm, actor)
        return updated

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if kind in ("alarm", "rescue_job"):
            if kind == "rescue_job":
                alarm = self.repository.get_entity(entity["data"].get("alarm_id"))
                equipment_id = alarm["data"].get("equipment_id") if alarm else None
            else:
                equipment_id = entity["data"].get("equipment_id")
            self._apply_equipment_safety_effect(equipment_id, actor)
        return updated

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
