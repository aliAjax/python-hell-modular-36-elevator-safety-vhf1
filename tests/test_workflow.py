import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def act(self, entity, action, data=None, version=None):
        return self.service.transition(self.actor, entity["id"], action, data or {}, version)

    def test_full_safety_flow(self):
        equipment = self.create("equipment", {"asset_no": "E-100", "equipment_type": "elevator", "location": "Tower A", "inspection_interval_days": 365})
        inspection = self.create("inspection", {"equipment_id": equipment["id"], "scheduled_at": "2026-09-27T09:00:00Z", "cycle_days": 365})
        inspection = self.act(inspection, "pass", {"findings": "normal"})
        self.assertEqual(inspection["status"], "passed")

        remediation = self.create("remediation", {"equipment_id": equipment["id"], "issue": "door alignment", "owner": "Maint", "due_at": "2026-10-01"})
        remediation = self.act(remediation, "submit_evidence", {"evidence": "IMG-1"})
        remediation = self.act(remediation, "verify", {})
        remediation = self.act(remediation, "close", {})
        self.assertEqual(remediation["status"], "closed")

        permit = self.create("permit", {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"})
        permit = self.act(permit, "request_review", {})
        permit = self.act(permit, "grant", {})
        self.assertEqual(permit["status"], "granted")

        alarm = self.create("alarm", {"equipment_id": equipment["id"], "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"})
        alarm = self.act(alarm, "dispatch", {"team": "Alpha"})
        job = self.create("rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "job-1", "team": "Alpha"})
        job = self.act(job, "arrive", {})
        job = self.act(job, "complete", {"outcome": "passenger freed"})
        alarm = self.act(alarm, "resolve", {"resolution": "passenger safe"})
        alarm = self.act(alarm, "close", {})
        self.assertEqual(alarm["status"], "closed")

    def test_alarm_locks_equipment_until_post_close_inspection(self):
        equipment = self.create("equipment", {"asset_no": "E-300", "equipment_type": "elevator", "location": "Tower A", "inspection_interval_days": 365})
        inspection = self.create("inspection", {"equipment_id": equipment["id"], "scheduled_at": "2026-09-27T09:00:00Z", "cycle_days": 365})
        inspection = self.act(inspection, "pass", {"findings": "normal", "passed_at": "2026-09-27T10:00:00Z"})

        maintenance = self.create("maintenance", {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-10-01"})
        active_maintenance = self.create("maintenance", {"equipment_id": equipment["id"], "work_type": "repair", "planned_at": "2026-09-28T09:00:00Z"})
        active_maintenance = self.act(active_maintenance, "start", {})
        blocked_permit = self.create("permit", {"equipment_id": equipment["id"], "purpose": "special_inspection", "requested_by": "queued"})
        permit = self.create("permit", {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"})
        permit = self.act(permit, "request_review", {})
        permit = self.act(permit, "grant", {})

        alarm = self.create("alarm", {"equipment_id": equipment["id"], "code": "TRAP", "occurred_at": "2026-09-28T10:00:00Z"})
        equipment = self.service.get(equipment["id"])
        self.assertEqual(equipment["status"], "out_of_service")
        self.assertTrue(equipment["data"]["safety_lock"]["active"])
        self.assertEqual(self.service.get(permit["id"])["status"], "revoked")
        self.assertEqual(self.service.get(blocked_permit["id"])["status"], "blocked")
        with self.assertRaisesRegex(ConflictError, alarm["id"]):
            self.act(blocked_permit, "request_review", {})
        with self.assertRaisesRegex(ConflictError, alarm["id"]):
            self.create("maintenance", {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-10-02"})
        with self.assertRaisesRegex(ConflictError, alarm["id"]):
            self.act(maintenance, "start", {})
        with self.assertRaisesRegex(ConflictError, alarm["id"]):
            self.act(active_maintenance, "complete", {"completed_at": "2026-09-28T10:15:00Z"})
        with self.assertRaisesRegex(ConflictError, alarm["id"]):
            self.create("permit", {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops-2"})

        alarm = self.act(alarm, "dispatch", {"team": "Alpha"})
        job = self.create("rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "job-300", "team": "Alpha"})
        job = self.act(job, "arrive", {})
        job = self.act(job, "complete", {"outcome": "passenger freed"})
        alarm = self.act(alarm, "resolve", {"resolution": "passenger safe"})
        alarm = self.act(alarm, "close", {"closed_at": "2026-09-28T11:00:00Z"})
        equipment = self.service.get(equipment["id"])
        self.assertEqual(equipment["status"], "out_of_service")
        self.assertFalse(equipment["data"]["safety_lock"]["active"])
        self.assertTrue(equipment["data"]["safety_lock"]["requires_recovery"])

        self.assertEqual(
            self.act(active_maintenance, "complete", {"completed_at": "2026-09-28T11:15:00Z"})["status"],
            "completed",
        )
        post_lock_maintenance = self.create("maintenance", {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-10-03"})
        self.act(post_lock_maintenance, "start", {})
        recovery_permit = self.create("permit", {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops-3"})
        recovery_permit = self.act(recovery_permit, "request_review", {})
        with self.assertRaises(ConflictError):
            self.act(recovery_permit, "grant", {})

        recheck = self.create("inspection", {"equipment_id": equipment["id"], "scheduled_at": "2026-09-28T12:00:00Z", "cycle_days": 365})
        recheck = self.act(recheck, "pass", {"findings": "safe after rescue", "passed_at": "2026-09-28T12:30:00Z"})
        recovery_permit = self.act(recovery_permit, "grant", {})
        equipment = self.service.get(equipment["id"])
        self.assertEqual(recovery_permit["status"], "granted")
        self.assertEqual(recovery_permit["data"]["qualified_inspection_id"], recheck["id"])
        self.assertEqual(equipment["status"], "in_service")
        self.assertFalse(equipment["data"]["safety_lock"]["requires_recovery"])

    def test_false_alarm_releases_lock_without_stopping_started_rescue(self):
        equipment = self.create("equipment", {"asset_no": "E-301", "equipment_type": "elevator", "location": "Tower B", "inspection_interval_days": 365})
        inspection = self.create("inspection", {"equipment_id": equipment["id"], "scheduled_at": "2026-09-27T09:00:00Z", "cycle_days": 365})
        self.act(inspection, "pass", {"findings": "normal", "passed_at": "2026-09-27T10:00:00Z"})
        permit = self.create("permit", {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"})
        permit = self.act(permit, "request_review", {})
        self.act(permit, "grant", {})

        alarm = self.create("alarm", {"equipment_id": equipment["id"], "code": "FALSE", "occurred_at": "2026-09-28T10:00:00Z"})
        alarm = self.act(alarm, "dispatch", {"team": "Bravo"})
        job = self.create("rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "job-301", "team": "Bravo"})
        job = self.act(job, "arrive", {})
        alarm = self.act(alarm, "mark_false", {})

        self.assertEqual(alarm["status"], "false_alarm")
        self.assertEqual(self.service.get(equipment["id"])["status"], "in_service")
        self.assertFalse(self.service.get(equipment["id"])["data"]["safety_lock"]["active"])
        self.assertEqual(self.service.get(permit["id"])["status"], "revoked")
        self.assertEqual(self.service.get(job["id"])["status"], "on_site")
        job = self.act(job, "complete", {"outcome": "team stood down after verification"})
        self.assertEqual(job["status"], "completed")

        replacement = self.create("permit", {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops-2"})
        replacement = self.act(replacement, "request_review", {})
        replacement = self.act(replacement, "grant", {})
        self.assertEqual(replacement["status"], "granted")

    def test_false_alarm_with_another_real_alarm_keeps_recovery_hold(self):
        equipment = self.create("equipment", {"asset_no": "E-302", "equipment_type": "elevator", "location": "Tower C", "inspection_interval_days": 365})
        real = self.create("alarm", {"equipment_id": equipment["id"], "code": "REAL", "occurred_at": "2026-09-28T10:00:00Z"})
        false = self.create("alarm", {"equipment_id": equipment["id"], "code": "FALSE", "occurred_at": "2026-09-28T10:05:00Z"})
        real = self.act(real, "dispatch", {"team": "Alpha"})
        false = self.act(false, "dispatch", {"team": "Bravo"})
        false_job = self.create("rescue_job", {"alarm_id": false["id"], "dedupe_key": "false-job", "team": "Bravo"})
        false_job = self.act(false_job, "arrive", {})
        false = self.act(false, "mark_false", {})
        self.assertEqual(self.service.get(equipment["id"])["status"], "out_of_service")
        self.assertTrue(self.service.get(equipment["id"])["data"]["safety_lock"]["active"])
        self.assertEqual(self.service.get(false_job["id"])["status"], "on_site")

        real_job = self.create("rescue_job", {"alarm_id": real["id"], "dedupe_key": "real-job", "team": "Alpha"})
        real_job = self.act(real_job, "arrive", {})
        real_job = self.act(real_job, "complete", {"outcome": "passenger freed"})
        real = self.act(real, "resolve", {"resolution": "safe"})
        real = self.act(real, "close", {"closed_at": "2026-09-28T11:00:00Z"})
        lock = self.service.get(equipment["id"])["data"]["safety_lock"]
        self.assertEqual(self.service.get(equipment["id"])["status"], "out_of_service")
        self.assertFalse(lock["active"])
        self.assertTrue(lock["requires_recovery"])
        self.assertIn(real["id"], lock["recovery_alarm_ids"])

    def test_component_replacement_requires_part_serial(self):
        equipment = self.create("equipment", {"asset_no": "E-200", "equipment_type": "escalator", "location": "Mall", "inspection_interval_days": 180})
        with self.assertRaises(Exception):
            self.create("maintenance", {"equipment_id": equipment["id"], "work_type": "component_replacement", "planned_at": "2026-10-01"})


if __name__ == "__main__":
    unittest.main()
