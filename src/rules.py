from datetime import datetime, timedelta, timezone

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _positive(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")
    if number <= 0:
        raise ValidationError(field + " must be positive")
    return number


def _validate_equipment(data, lookup):
    asset_no = str(data.get("asset_no", "")).strip()
    if not asset_no:
        raise ValidationError("asset_no is required")
    if _find_one(lookup, "equipment", "asset_no", asset_no):
        raise ConflictError("equipment asset_no already exists: " + asset_no)
    _positive(data.get("inspection_interval_days"), "inspection_interval_days")


def _validate_inspection(data, lookup):
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("inspection requires equipment")
    try:
        datetime.fromisoformat(str(data.get("scheduled_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("scheduled_at must be ISO-8601")
    _positive(data.get("cycle_days"), "cycle_days")


def _validate_maintenance(data, lookup):
    equipment = _equipment_by_id(lookup, data.get("equipment_id"))
    if not equipment:
        raise ValidationError("maintenance requires equipment")
    if data.get("work_type") not in ("routine", "repair", "component_replacement", "modernization"):
        raise ValidationError("invalid work_type")
    if data.get("work_type") == "component_replacement" and not data.get("part_serial"):
        raise ValidationError("part_serial is required for component replacement")
    _require_no_active_alarm(data.get("equipment_id"), lookup, "maintenance request")


def _validate_alarm(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("alarm requires equipment")
    for alarm in _all(lookup, "alarm"):
        if (
            alarm["data"].get("equipment_id") == data.get("equipment_id")
            and alarm["data"].get("code") == data.get("code")
            and alarm["status"] not in ("closed", "false_alarm")
        ):
            raise ConflictError("active alarm already exists for equipment and code")


def _validate_rescue(data, lookup):
    alarm = _find_one(lookup, "alarm", "id", data.get("alarm_id"))
    if not alarm or alarm["status"] in ("closed", "false_alarm"):
        raise ValidationError("rescue_job requires an active alarm")
    key = data.get("dedupe_key")
    for job in _all(lookup, "rescue_job"):
        if job["data"].get("dedupe_key") == key and job["status"] not in ("completed", "aborted"):
            raise ConflictError("active rescue job already exists for dedupe_key")


def _validate_remediation(data, lookup):
    if not data.get("equipment_id") and not data.get("alarm_id"):
        raise ValidationError("remediation requires equipment_id or alarm_id")
    issue = str(data.get("issue", "")).strip()
    for item in _all(lookup, "remediation"):
        if item["data"].get("equipment_id") == data.get("equipment_id") and item["data"].get("issue") == issue and item["status"] not in ("closed",):
            raise ConflictError("open remediation already exists for issue")


def _equipment_by_id(lookup, equipment_id):
    return _find_one(lookup, "equipment", "id", equipment_id)


def _active_alarms(lookup, equipment_id, exclude_id=None):
    return [
        alarm
        for alarm in _all(lookup, "alarm")
        if alarm["data"].get("equipment_id") == equipment_id
        and alarm["id"] != exclude_id
        and alarm["status"] not in ("closed", "false_alarm")
    ]


def _blocking_alarm(lookup, equipment_id, exclude_id=None):
    alarms = _active_alarms(lookup, equipment_id, exclude_id)
    if not alarms:
        return None
    return sorted(
        alarms,
        key=lambda alarm: (str(alarm["data"].get("occurred_at", "")), alarm["id"]),
    )[0]


def _require_no_active_alarm(equipment_id, lookup, action, exclude_id=None):
    alarm = _blocking_alarm(lookup, equipment_id, exclude_id)
    if alarm:
        raise ConflictError(
            "%s blocked by active alarm %s"
            % (action, alarm["id"])
        )
    return alarm


def _safety_lock(equipment):
    lock = dict(equipment["data"].get("safety_lock") or {})
    lock.setdefault("alarm_ids", [])
    return lock


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _alarm_locks_equipment(equipment, alarm, lookup, at=None):
    at = at or _now_iso()
    lock = _safety_lock(equipment)
    if not lock.get("active") and not lock.get("requires_recovery"):
        lock.update(
            {
                "active": True,
                "previous_status": equipment["status"],
                "locked_at": at,
                "alarm_ids": [alarm["id"]],
                "recovery_alarm_ids": [],
            }
        )
        lock.pop("requires_recovery", None)
        lock.pop("recovery_required_at", None)
    else:
        if alarm["id"] not in lock["alarm_ids"]:
            lock["alarm_ids"].append(alarm["id"])
            lock["alarm_ids"].sort()
        lock["active"] = True
        lock.pop("requires_recovery", None)
        lock.pop("recovery_required_at", None)
    return lock

def _alarm_create_effects(equipment_id, alarm, lookup):
    equipment = _equipment_by_id(lookup, equipment_id)
    if not equipment:
        return [], equipment
    effects = []
    lock = _alarm_locks_equipment(equipment, alarm, lookup)
    equipment_data = dict(equipment["data"])
    equipment_data["safety_lock"] = lock
    if equipment["status"] != "out_of_service":
        effects.append(
            {
                "entity": equipment,
                "status": "out_of_service",
                "data": equipment_data,
                "action": "safety_lock",
                "detail": {"alarm_id": alarm["id"]},
            }
        )
    else:
        effects.append(
            {
                "entity": equipment,
                "status": "out_of_service",
                "data": equipment_data,
                "action": "safety_lock",
                "detail": {"alarm_id": alarm["id"], "already_out_of_service": True},
            }
        )

    for permit in [
        item
        for item in _all(lookup, "permit")
        if item["data"].get("equipment_id") == equipment_id
        and item["status"] in ("pending_review", "granted")
    ]:
        permit_data = dict(permit["data"])
        permit_data.update(
            {
                "revoked_reason": "revoked by active alarm " + alarm["id"],
                "revoked_by_alarm_id": alarm["id"],
                "revoked_at": _now_iso(),
            }
        )
        effects.append(
            {
                "entity": permit,
                "status": "revoked",
                "data": permit_data,
                "action": "revoke",
                "detail": {"alarm_id": alarm["id"], "automatic": True},
            }
        )
    return effects, equipment


def _validate_permit(data, lookup):
    equipment = _equipment_by_id(lookup, data.get("equipment_id"))
    if not equipment:
        raise ValidationError("permit requires equipment")
    if data.get("purpose") not in ("return_to_service", "special_inspection", "temporary_operation"):
        raise ValidationError("invalid permit purpose")
    _require_no_active_alarm(data.get("equipment_id"), lookup, "permit request")


def _request_permit_review(actor, entity, data, lookup):
    _require_no_active_alarm(
        entity["data"].get("equipment_id"), lookup, "permit request"
    )
    return {}, []


def _grant_permit(actor, entity, data, lookup):
    equipment = _equipment_by_id(lookup, entity["data"].get("equipment_id"))
    if not equipment:
        raise ConflictError("permit requires equipment")
    _require_no_active_alarm(equipment["id"], lookup, "permit grant")
    lock = _safety_lock(equipment)
    if equipment["status"] == "out_of_service":
        if entity["data"].get("purpose") != "return_to_service" or not lock.get("requires_recovery"):
            raise ConflictError("permit can only be granted for a serviceable equipment")
    elif equipment["status"] not in ("in_service", "suspended"):
        raise ConflictError("permit can only be granted for a serviceable equipment")

    inspections = [
        item
        for item in _all(lookup, "inspection")
        if item["data"].get("equipment_id") == equipment["id"] and item["status"] == "passed"
    ]
    qualified = None
    required_at = lock.get("recovery_required_at") if lock.get("requires_recovery") else None
    required_dt = _parse_iso(required_at)
    for inspection in inspections:
        passed_at = inspection["data"].get("passed_at")
        if required_dt and not _parse_iso(passed_at):
            continue
        if required_dt and _parse_iso(passed_at) <= required_dt:
            continue
        if qualified is None or str(passed_at or "") > str(qualified["data"].get("passed_at", "")):
            qualified = inspection
    if not qualified:
        if required_dt:
            raise ConflictError("permit requires a passed inspection after alarm closure")
        raise ConflictError("permit requires a passed inspection")
    if [
        item
        for item in _all(lookup, "remediation")
        if item["data"].get("equipment_id") == equipment["id"] and item["status"] != "closed"
    ]:
        raise ConflictError("permit blocked by open remediation")

    patch = {
        "granted_by": actor.user_id,
        "granted_at": _now_iso(),
        "qualified_inspection_id": qualified["id"],
    }
    effects = []
    if equipment["status"] == "out_of_service" and entity["data"].get("purpose") == "return_to_service":
        equipment_data = dict(equipment["data"])
        released_lock = dict(lock)
        released_lock.update(
            {
                "active": False,
                "requires_recovery": False,
                "recovery_alarm_ids": [],
                "recovered_at": patch["granted_at"],
                "recovered_by_permit_id": entity["id"],
            }
        )
        equipment_data["safety_lock"] = released_lock
        effects.append(
            {
                "entity": equipment,
                "status": "in_service",
                "data": equipment_data,
                "action": "return_to_service",
                "detail": {"permit_id": entity["id"], "inspection_id": qualified["id"]},
            }
        )
    return patch, effects


def _pass_inspection(actor, entity, data, lookup):
    passed_at = data.get("passed_at") or _now_iso()
    if not _parse_iso(passed_at):
        raise ValidationError("passed_at must be ISO-8601")
    return {"passed_by": actor.user_id, "passed_at": passed_at}, []


def _maintenance_transition(actor, entity, data, lookup):
    action = "complete" if entity["status"] == "in_progress" else "start"
    _require_no_active_alarm(
        entity["data"].get("equipment_id"), lookup, "maintenance " + action
    )
    return {}, []


def _mark_alarm_false(actor, entity, data, lookup):
    at = data.get("false_alarm_at") or _now_iso()
    if not _parse_iso(at):
        raise ValidationError("false_alarm_at must be ISO-8601")
    equipment = _equipment_by_id(lookup, entity["data"].get("equipment_id"))
    if not equipment:
        raise ConflictError("alarm requires equipment")
    lock = _safety_lock(equipment)
    remaining = _active_alarms(lookup, equipment["id"], exclude_id=entity["id"])
    lock["alarm_ids"] = [alarm_id for alarm_id in lock.get("alarm_ids", []) if alarm_id != entity["id"]]
    for alarm in remaining:
        if alarm["id"] not in lock["alarm_ids"]:
            lock["alarm_ids"].append(alarm["id"])
    lock["alarm_ids"].sort()

    if remaining:
        lock["active"] = True
        equipment_status = "out_of_service"
        detail = {"alarm_id": entity["id"], "remaining_alarm_ids": lock["alarm_ids"]}
    else:
        recovery_ids = lock.get("recovery_alarm_ids", [])
        needs_recovery = bool(recovery_ids)
        lock["active"] = False
        lock["false_alarm_at"] = at
        if needs_recovery:
            lock["requires_recovery"] = True
            lock["recovery_required_at"] = at
        else:
            lock.pop("requires_recovery", None)
            lock.pop("recovery_required_at", None)
        equipment_status = "out_of_service" if needs_recovery else lock.get("previous_status", "in_service")
        if equipment_status not in ("in_service", "suspended", "out_of_service"):
            equipment_status = "in_service"
        detail = {
            "alarm_id": entity["id"],
            "released": not needs_recovery,
            "recovery_alarm_ids": recovery_ids,
        }

    equipment_data = dict(equipment["data"])
    equipment_data["safety_lock"] = lock
    effects = [
        {
            "entity": equipment,
            "status": equipment_status,
            "data": equipment_data,
            "action": "safety_lock_release" if not remaining and not lock.get("requires_recovery") else "safety_lock",
            "detail": detail,
        }
    ]
    patch = {"marked_false_by": actor.user_id, "false_alarm_at": at}
    return patch, effects


def _close_alarm(actor, entity, data, lookup):
    jobs = [j for j in _all(lookup, "rescue_job") if j["data"].get("alarm_id") == entity["id"]]
    if not jobs or any(job["status"] not in ("completed", "aborted") for job in jobs):
        raise ConflictError("alarm cannot close before rescue jobs are complete")
    at = data.get("closed_at") or _now_iso()
    if not _parse_iso(at):
        raise ValidationError("closed_at must be ISO-8601")
    equipment = _equipment_by_id(lookup, entity["data"].get("equipment_id"))
    if not equipment:
        raise ConflictError("alarm requires equipment")
    lock = _safety_lock(equipment)
    remaining = _active_alarms(lookup, equipment["id"], exclude_id=entity["id"])
    recovery_ids = lock.setdefault("recovery_alarm_ids", [])
    if entity["id"] not in recovery_ids:
        recovery_ids.append(entity["id"])
    recovery_ids.sort()
    lock["alarm_ids"] = sorted(alarm["id"] for alarm in remaining)
    if remaining:
        lock["active"] = True
        lock.pop("requires_recovery", None)
        lock.pop("recovery_required_at", None)
    else:
        lock["active"] = False
        lock["requires_recovery"] = True
        lock["recovery_required_at"] = at
    equipment_data = dict(equipment["data"])
    equipment_data["safety_lock"] = lock
    effects = [
        {
            "entity": equipment,
            "status": "out_of_service",
            "data": equipment_data,
            "action": "safety_lock_hold",
            "detail": {
                "alarm_id": entity["id"],
                "requires_qualified_inspection": not bool(remaining),
                "remaining_alarm_ids": [alarm["id"] for alarm in remaining],
            },
        }
    ]
    patch = {"closed_by": actor.user_id, "closed_at": at}
    return patch, effects


def _verify_remediation(actor, entity, data, lookup):
    if not entity["data"].get("evidence"):
        raise ValidationError("remediation evidence is required before verification")
    return {"verified_by": actor.user_id}, []


class RuleEngine:
    ALIASES = {
        "equipments": "equipment", "inspections": "inspection", "maintenances": "maintenance",
        "alarms": "alarm", "rescue_jobs": "rescue_job", "remediations": "remediation",
        "permits": "permit",
    }
    INITIAL_STATUS = {
        "equipment": "in_service", "inspection": "scheduled", "maintenance": "planned",
        "alarm": "received", "rescue_job": "dispatched", "remediation": "open",
        "permit": "blocked",
    }
    TRANSITIONS = {
        "equipment": {
            "suspend": (("in_service",), "suspended"),
            "out_of_service": (("in_service", "suspended"), "out_of_service"),
            "return_to_service": (("suspended",), "in_service"),
        },
        "inspection": {
            "pass": (("scheduled",), "passed"),
            "fail": (("scheduled",), "failed"),
            "reschedule": (("failed",), "scheduled"),
        },
        "maintenance": {
            "start": (("planned",), "in_progress"),
            "complete": (("in_progress",), "completed"),
        },
        "alarm": {
            "dispatch": (("received",), "dispatched"),
            "mark_false": (("received", "dispatched"), "false_alarm"),
            "resolve": (("dispatched",), "resolved"),
            "close": (("resolved",), "closed"),
        },
        "rescue_job": {
            "arrive": (("dispatched",), "on_site"),
            "complete": (("on_site",), "completed"),
            "abort": (("dispatched", "on_site"), "aborted"),
        },
        "remediation": {
            "submit_evidence": (("open",), "evidence_submitted"),
            "verify": (("evidence_submitted",), "verified"),
            "reject": (("evidence_submitted",), "open"),
            "close": (("verified",), "closed"),
        },
        "permit": {
            "request_review": (("blocked",), "pending_review"),
            "grant": (("pending_review",), "granted"),
            "revoke": (("granted", "pending_review"), "revoked"),
            "expire": (("granted",), "expired"),
        },
    }
    CREATE_REQUIRED = {
        "equipment": ("asset_no", "equipment_type", "location", "inspection_interval_days"),
        "inspection": ("equipment_id", "scheduled_at", "cycle_days"),
        "maintenance": ("equipment_id", "work_type", "planned_at"),
        "alarm": ("equipment_id", "code", "occurred_at"),
        "rescue_job": ("alarm_id", "dedupe_key", "team"),
        "remediation": ("issue", "owner", "due_at"),
        "permit": ("equipment_id", "purpose", "requested_by"),
    }
    ACTION_REQUIRED = {
        ("inspection", "pass"): ("findings",),
        ("inspection", "fail"): ("findings",),
        ("maintenance", "complete"): ("completed_at",),
        ("rescue_job", "complete"): ("outcome",),
        ("remediation", "submit_evidence"): ("evidence",),
        ("alarm", "resolve"): ("resolution",),
        ("permit", "revoke"): ("reason",),
    }
    CREATE_ROLES = {
        "equipment": ("admin", "inspector"),
        "inspection": ("admin", "inspector"),
        "maintenance": ("admin", "maintenance"),
        "alarm": ("admin", "dispatcher", "inspector"),
        "rescue_job": ("admin", "dispatcher"),
        "remediation": ("admin", "inspector", "maintenance"),
        "permit": ("admin", "inspector"),
    }
    ROLE_ACTIONS = {
        "suspend": ("admin", "inspector"),
        "out_of_service": ("admin", "inspector"),
        "return_to_service": ("admin", "inspector"),
        "pass": ("admin", "inspector"),
        "fail": ("admin", "inspector"),
        "reschedule": ("admin", "inspector"),
        "start": ("admin", "maintenance"),
        "complete": ("admin", "maintenance", "dispatcher"),
        "dispatch": ("admin", "dispatcher"),
        "mark_false": ("admin", "dispatcher", "inspector"),
        "resolve": ("admin", "dispatcher"),
        "close": ("admin", "dispatcher", "inspector"),
        "arrive": ("admin", "dispatcher"),
        "abort": ("admin", "dispatcher"),
        "submit_evidence": ("admin", "maintenance", "inspector"),
        "verify": ("admin", "inspector"),
        "reject": ("admin", "inspector"),
        "request_review": ("admin", "inspector"),
        "grant": ("admin", "inspector"),
        "revoke": ("admin", "inspector"),
        "expire": ("admin", "inspector"),
    }
    CUSTOM_CREATE = {
        "equipment": lambda a, d, l: _validate_equipment(d, l),
        "inspection": lambda a, d, l: _validate_inspection(d, l),
        "maintenance": lambda a, d, l: _validate_maintenance(d, l),
        "alarm": lambda a, d, l: _validate_alarm(d, l),
        "rescue_job": lambda a, d, l: _validate_rescue(d, l),
        "remediation": lambda a, d, l: _validate_remediation(d, l),
        "permit": lambda a, d, l: _validate_permit(d, l),
    }
    CUSTOM_TRANSITIONS = {
        ("maintenance", "start"): _maintenance_transition,
        ("maintenance", "complete"): _maintenance_transition,
        ("inspection", "pass"): _pass_inspection,
        ("alarm", "mark_false"): _mark_alarm_false,
        ("alarm", "close"): _close_alarm,
        ("permit", "request_review"): _request_permit_review,
        ("permit", "grant"): _grant_permit,
        ("remediation", "verify"): _verify_remediation,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def create_side_effects(self, kind, entity, lookup=None):
        kind = self.normalize_kind(kind)
        if kind == "alarm":
            effects, _equipment = _alarm_create_effects(entity["data"].get("equipment_id"), entity, lookup)
            return effects
        return []

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra, effects = custom(actor, entity, data, lookup) if custom else ({}, [])
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch, effects
