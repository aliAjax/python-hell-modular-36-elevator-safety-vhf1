import hashlib
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
        existing_entity = self.repository.get_entity(entity_id)
        if existing_entity:
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        preview = {
            "id": entity_id,
            "kind": kind,
            "status": status,
            "version": 1,
            "data": payload,
            "created_by": actor.user_id,
        }
        effects = self.rules.create_side_effects(kind, preview, self._lookup)
        if effects:
            self._apply_create_effects(actor, preview, effects)
            entity = self.repository.get_entity(entity_id)
        else:
            entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
            self.audit.record(entity_id, actor, "create", None, entity["status"], {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _apply_create_effects(self, actor, entity, effects):
        with self.repository.transaction() as connection:
            self.repository._create_row(
                connection,
                entity["id"],
                entity["kind"],
                entity["status"],
                entity["data"],
                entity["created_by"],
            )
            self.repository._append_audit_row(
                connection,
                entity["id"],
                actor.user_id,
                actor.role,
                "create",
                None,
                entity["status"],
                {"kind": entity["kind"]},
            )
            for effect in effects:
                target = effect["entity"]
                self.repository._update_row(
                    connection,
                    target["id"],
                    target["version"],
                    effect["status"],
                    effect["data"],
                )
                self.repository._append_audit_row(
                    connection,
                    target["id"],
                    actor.user_id,
                    actor.role,
                    effect["action"],
                    target["status"],
                    effect["status"],
                    effect.get("detail", {}),
                )
        return self.repository.get_entity(entity["id"])

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch, effects = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        if effects:
            with self.repository.transaction() as connection:
                self.repository._update_row(connection, entity_id, expected, next_status, merged)
                self.repository._append_audit_row(
                    connection,
                    entity_id,
                    actor.user_id,
                    actor.role,
                    action,
                    entity["status"],
                    next_status,
                    {"patch": patch},
                )
                for effect in effects:
                    target = effect["entity"]
                    self.repository._update_row(
                        connection,
                        target["id"],
                        target["version"],
                        effect["status"],
                        effect["data"],
                    )
                    self.repository._append_audit_row(
                        connection,
                        target["id"],
                        actor.user_id,
                        actor.role,
                        effect["action"],
                        target["status"],
                        effect["status"],
                        effect.get("detail", {}),
                    )
            updated = self.repository.get_entity(entity_id)
        else:
            updated = self.repository.update_entity(entity_id, expected, next_status, merged)
            self.audit.record(
                entity_id,
                actor,
                action,
                entity["status"],
                updated["status"],
                {"patch": patch},
            )
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
