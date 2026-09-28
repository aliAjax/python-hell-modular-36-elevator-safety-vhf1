from datetime import datetime, timezone

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


ACTIVE_ALARM_STATUSES = {"received", "dispatched", "resolved"}
ACTIVE_RESCUE_STATUSES = {"dispatched", "on_site"}
TERMINAL_ALARM_STATUSES = {"closed", "false_alarm"}


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


def _parse_utc(value, field):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValidationError(field + " must be ISO-8601")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _equipment_alarms(lookup, equipment_id):
    return [
        alarm
        for alarm in _all(lookup, "alarm")
        if alarm["data"].get("equipment_id") == equipment_id
    ]


def _active_safety_block(lookup, equipment_id, exclude_alarm_id=None):
    """Return the alarm/rescue that must keep the equipment safety-locked."""
    alarms = _equipment_alarms(lookup, equipment_id)
    alarm_by_id = {alarm["id"]: alarm for alarm in alarms}
    active_alarms = [
        alarm for alarm in alarms
        if alarm["id"] != exclude_alarm_id and alarm["status"] in ACTIVE_ALARM_STATUSES
    ]
    if active_alarms:
        alarm = sorted(active_alarms, key=lambda item: item["created_at"] + item["id"])[0]
        return {"type": "alarm", "alarm": alarm}

    jobs = [
        job for job in _all(lookup, "rescue_job")
        if job["status"] in ACTIVE_RESCUE_STATUSES
        and job["data"].get("alarm_id") in alarm_by_id
    ]
    if jobs:
        job = sorted(jobs, key=lambda item: item["created_at"] + item["id"])[0]
        return {"type": "rescue", "alarm": alarm_by_id[job["data"].get("alarm_id")], "job": job}
    return None


def _ensure_not_safety_locked(lookup, equipment_id, label, exclude_alarm_id=None):
    block = _active_safety_block(lookup, equipment_id, exclude_alarm_id=exclude_alarm_id)
    if block:
        alarm = block["alarm"]
        alarm_id = alarm["id"]
        code = alarm["data"].get("code") or alarm["status"]
        if block["type"] == "rescue":
            raise ConflictError(
                "%s blocked by active rescue job %s for alarm %s (%s)"
                % (label, block["job"]["id"], alarm_id, code)
            )
        raise ConflictError("%s blocked by active alarm %s (%s)" % (label, alarm_id, code))


def _ensure_recovery_ready(lookup, equipment_id, label, exclude_alarm_id=None):
    _ensure_not_safety_locked(lookup, equipment_id, label, exclude_alarm_id=exclude_alarm_id)
    required_alarm = _requires_release_inspection(lookup, equipment_id)
    if required_alarm:
        raise ConflictError(
            "%s blocked until a qualified inspection passes after alarm %s is released"
            % (label, required_alarm["id"])
        )


def _requires_release_inspection(lookup, equipment_id):
    alarms = [
        alarm for alarm in _equipment_alarms(lookup, equipment_id)
        if alarm["status"] == "closed"
    ]
    if not alarms:
        return None
    latest_closed = sorted(alarms, key=lambda item: (item["data"].get("released_at") or item["updated_at"], item["id"]))[-1]
    inspections = [
        inspection
        for inspection in _all(lookup, "inspection")
        if inspection["data"].get("equipment_id") == equipment_id
        and inspection["status"] == "passed"
    ]
    released_at = _parse_utc(latest_closed["data"].get("released_at") or latest_closed["updated_at"], "released_at")
    for inspection in inspections:
        passed_at = inspection["data"].get("passed_at") or inspection["updated_at"]
        if _parse_utc(passed_at, "passed_at") > released_at:
            return None
    return latest_closed


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
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("maintenance requires equipment")
    _ensure_recovery_ready(lookup, equipment["id"], "maintenance")
    if data.get("work_type") not in ("routine", "repair", "component_replacement", "modernization"):
        raise ValidationError("invalid work_type")
    if data.get("work_type") == "component_replacement" and not data.get("part_serial"):
        raise ValidationError("part_serial is required for component replacement")


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
    if not alarm or alarm["status"] in TERMINAL_ALARM_STATUSES:
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


def _validate_permit(data, lookup):
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("permit requires equipment")
    _ensure_recovery_ready(lookup, equipment["id"], "permit application")
    if data.get("purpose") not in ("return_to_service", "special_inspection", "temporary_operation"):
        raise ValidationError("invalid permit purpose")


def _request_permit_review(actor, entity, data, lookup):
    _ensure_recovery_ready(lookup, entity["data"].get("equipment_id"), "permit application")
    return {}


def _return_equipment_to_service(actor, entity, data, lookup):
    _ensure_not_safety_locked(lookup, entity["id"], "equipment return to service")
    patch = {}
    if entity["data"].get("recovery_required_alarm_id"):
        permits = [
            permit
            for permit in _all(lookup, "permit")
            if permit["data"].get("equipment_id") == entity["id"]
        ]
        if not any(permit["status"] == "granted" for permit in permits):
            raise ConflictError("return to service requires a granted recovery permit")
        patch["recovery_required_alarm_id"] = None
        patch["pre_lock_status"] = None
    return patch


def _pass_inspection(actor, entity, data, lookup):
    return {
        "passed_by": actor.user_id,
        "passed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
    }


def _start_maintenance(actor, entity, data, lookup):
    _ensure_recovery_ready(lookup, entity["data"].get("equipment_id"), "maintenance start")
    return {}


def _complete_maintenance(actor, entity, data, lookup):
    _ensure_recovery_ready(lookup, entity["data"].get("equipment_id"), "maintenance completion")
    return {}


def _mark_alarm_terminal(actor, entity, data, lookup):
    return {"released_at": datetime.now(timezone.utc).isoformat(timespec="microseconds")}


def _grant_permit(actor, entity, data, lookup):
    equipment_id = entity["data"].get("equipment_id")
    _ensure_not_safety_locked(lookup, equipment_id, "permit grant")
    equipment = _find_one(lookup, "equipment", "id", equipment_id)
    if not equipment or equipment["status"] not in ("in_service", "suspended", "out_of_service"):
        raise ConflictError("permit can only be granted for a serviceable equipment")
    required_alarm = _requires_release_inspection(lookup, equipment_id)
    if required_alarm:
        raise ConflictError(
            "permit requires a passed inspection after alarm %s was released"
            % required_alarm["id"]
        )
    inspections = [i for i in _all(lookup, "inspection") if i["data"].get("equipment_id") == equipment_id and i["status"] == "passed"]
    if not inspections:
        raise ConflictError("permit requires a passed inspection")
    if [r for r in _all(lookup, "remediation") if r["data"].get("equipment_id") == equipment_id and r["status"] != "closed"]:
        raise ConflictError("permit blocked by open remediation")
    return {"granted_by": actor.user_id, "granted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def _verify_remediation(actor, entity, data, lookup):
    if not entity["data"].get("evidence"):
        raise ValidationError("remediation evidence is required before verification")
    return {"verified_by": actor.user_id}


def _complete_rescue(actor, entity, data, lookup):
    jobs = [j for j in _all(lookup, "rescue_job") if j["data"].get("alarm_id") == entity["id"]]
    if not jobs or any(job["status"] not in ("completed", "aborted") for job in jobs):
        raise ConflictError("alarm cannot close before rescue jobs are complete")
    return {"resolved_by": actor.user_id}


def _close_alarm(actor, entity, data, lookup):
    patch = _complete_rescue(actor, entity, data, lookup)
    patch.update(_mark_alarm_terminal(actor, entity, data, lookup))
    return patch


class RuleEngine:
    ACTIVE_ALARM_STATUSES = ACTIVE_ALARM_STATUSES
    ACTIVE_RESCUE_STATUSES = ACTIVE_RESCUE_STATUSES
    TERMINAL_ALARM_STATUSES = TERMINAL_ALARM_STATUSES
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
            "return_to_service": (("suspended", "out_of_service"), "in_service"),
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
        ("equipment", "return_to_service"): _return_equipment_to_service,
        ("inspection", "pass"): _pass_inspection,
        ("maintenance", "start"): _start_maintenance,
        ("maintenance", "complete"): _complete_maintenance,
        ("permit", "request_review"): _request_permit_review,
        ("permit", "grant"): _grant_permit,
        ("remediation", "verify"): _verify_remediation,
        ("alarm", "close"): _close_alarm,
        ("alarm", "mark_false"): _mark_alarm_terminal,
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
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
