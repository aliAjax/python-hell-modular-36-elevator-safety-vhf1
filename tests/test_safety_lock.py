import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class SafetyLockTest(unittest.TestCase):
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

    def get(self, entity):
        return self.service.get(entity["id"])

    def equipment(self, asset_no="E-SAFE"):
        return self.create(
            "equipment",
            {
                "asset_no": asset_no,
                "equipment_type": "elevator",
                "location": "Tower A",
                "inspection_interval_days": 365,
            },
        )

    def inspection(self, equipment):
        inspection = self.create(
            "inspection",
            {
                "equipment_id": equipment["id"],
                "scheduled_at": "2026-09-28T09:00:00Z",
                "cycle_days": 365,
            },
        )
        return self.act(inspection, "pass", {"findings": "normal"})

    def real_alarm_flow(self, equipment, code="TRAP"):
        alarm = self.create(
            "alarm",
            {
                "equipment_id": equipment["id"],
                "code": code,
                "occurred_at": "2026-09-28T10:00:00Z",
            },
        )
        alarm = self.act(alarm, "dispatch", {"team": "Alpha"})
        job = self.create(
            "rescue_job",
            {"alarm_id": alarm["id"], "dedupe_key": "job-" + code, "team": "Alpha"},
        )
        job = self.act(job, "arrive", {})
        job = self.act(job, "complete", {"outcome": "passenger freed"})
        alarm = self.act(alarm, "resolve", {"resolution": "passenger safe"})
        alarm = self.act(alarm, "close", {})
        return alarm, job

    def test_active_alarm_locks_equipment_revokes_permit_and_blocks_work(self):
        equipment = self.equipment()
        self.inspection(equipment)

        active_maintenance = self.create(
            "maintenance",
            {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-09-28T11:00:00Z"},
        )
        active_maintenance = self.act(active_maintenance, "start", {})

        permit = self.create(
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.act(permit, "request_review", {})
        permit = self.act(permit, "grant", {})

        alarm = self.create(
            "alarm",
            {"equipment_id": equipment["id"], "code": "DOOR-JAM", "occurred_at": "2026-09-28T10:00:00Z"},
        )

        equipment = self.get(equipment)
        self.assertEqual(equipment["status"], "suspended")
        self.assertEqual(equipment["data"]["locked_by_alarm_id"], alarm["id"])
        self.assertEqual(self.get(permit)["status"], "revoked")
        self.assertEqual(self.get(permit)["data"]["blocked_by_alarm_id"], alarm["id"])

        with self.assertRaisesRegex(ConflictError, alarm["id"]):
            self.act(active_maintenance, "complete", {"completed_at": "2026-09-28T12:00:00Z"})
        with self.assertRaisesRegex(ConflictError, "DOOR-JAM"):
            self.create(
                "maintenance",
                {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-09-29T11:00:00Z"},
            )
        with self.assertRaisesRegex(ConflictError, alarm["id"]):
            self.create(
                "permit",
                {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
            )

    def test_false_alarm_releases_lock_after_started_rescue_without_stopping_rescue(self):
        equipment = self.equipment()
        self.inspection(equipment)
        alarm = self.create(
            "alarm",
            {"equipment_id": equipment["id"], "code": "FALSE-1", "occurred_at": "2026-09-28T10:00:00Z"},
        )
        alarm = self.act(alarm, "dispatch", {"team": "Alpha"})
        job = self.create(
            "rescue_job",
            {"alarm_id": alarm["id"], "dedupe_key": "false-job", "team": "Alpha"},
        )
        job = self.act(job, "arrive", {})

        alarm = self.act(alarm, "mark_false", {})

        self.assertEqual(self.get(equipment)["status"], "suspended")
        self.assertEqual(self.get(equipment)["data"]["locked_by_alarm_id"], alarm["id"])
        with self.assertRaises(ValidationError):
            self.create(
                "rescue_job",
                {"alarm_id": alarm["id"], "dedupe_key": "new-job", "team": "Bravo"},
            )

        job = self.act(job, "complete", {"outcome": "all clear"})
        self.assertEqual(job["status"], "completed")
        self.assertEqual(self.get(equipment)["status"], "in_service")
        self.assertIsNone(self.get(equipment)["data"]["locked_by_alarm_id"])
        self.assertIsNone(self.get(equipment)["data"].get("recovery_required_alarm_id"))

        maintenance = self.create(
            "maintenance",
            {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-09-29T11:00:00Z"},
        )
        self.assertEqual(maintenance["status"], "planned")

    def test_real_rescue_requires_post_release_inspection_and_permit_before_return(self):
        equipment = self.equipment()
        old_inspection = self.inspection(equipment)
        alarm, _job = self.real_alarm_flow(equipment)

        equipment = self.get(equipment)
        self.assertEqual(equipment["status"], "suspended")
        self.assertEqual(equipment["data"]["recovery_required_alarm_id"], alarm["id"])
        self.assertIsNone(equipment["data"]["locked_by_alarm_id"])

        with self.assertRaisesRegex(ConflictError, "qualified inspection"):
            self.create(
                "maintenance",
                {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-09-29T11:00:00Z"},
            )
        with self.assertRaisesRegex(ConflictError, alarm["id"]):
            self.create(
                "permit",
                {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
            )
        with self.assertRaisesRegex(ConflictError, "granted recovery permit"):
            self.act(equipment, "return_to_service", {})

        inspection = self.create(
            "inspection",
            {
                "equipment_id": equipment["id"],
                "scheduled_at": "2026-09-29T09:00:00Z",
                "cycle_days": 365,
            },
        )
        inspection = self.act(inspection, "pass", {"findings": "safe after rescue"})
        self.assertGreater(inspection["data"]["passed_at"], old_inspection["data"]["passed_at"])

        permit = self.create(
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.act(permit, "request_review", {})
        permit = self.act(permit, "grant", {})
        self.assertEqual(permit["status"], "granted")
        self.assertEqual(self.get(equipment)["status"], "suspended")

        equipment = self.act(equipment, "return_to_service", {})
        self.assertEqual(equipment["status"], "in_service")
        self.assertIsNone(equipment["data"]["recovery_required_alarm_id"])

        maintenance = self.create(
            "maintenance",
            {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-09-30T11:00:00Z"},
        )
        self.assertEqual(maintenance["status"], "planned")

    def test_later_false_alarm_does_not_clear_unfinished_real_alarm_recovery(self):
        equipment = self.equipment()
        self.inspection(equipment)
        real_alarm, _job = self.real_alarm_flow(equipment, code="REAL")

        false_alarm = self.create(
            "alarm",
            {"equipment_id": equipment["id"], "code": "FALSE-2", "occurred_at": "2026-09-30T10:00:00Z"},
        )
        false_alarm = self.act(false_alarm, "dispatch", {"team": "Bravo"})
        false_job = self.create(
            "rescue_job",
            {"alarm_id": false_alarm["id"], "dedupe_key": "false-job-2", "team": "Bravo"},
        )
        false_job = self.act(false_job, "arrive", {})
        false_job = self.act(false_job, "complete", {"outcome": "all clear"})
        false_alarm = self.act(false_alarm, "mark_false", {})

        equipment = self.get(equipment)
        self.assertEqual(equipment["status"], "suspended")
        self.assertEqual(equipment["data"]["recovery_required_alarm_id"], real_alarm["id"])
        self.assertIsNone(equipment["data"]["locked_by_alarm_id"])
        with self.assertRaisesRegex(ConflictError, "qualified inspection"):
            self.create(
                "maintenance",
                {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-10-01T11:00:00Z"},
            )


if __name__ == "__main__":
    unittest.main()
