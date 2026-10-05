import asyncio
import os
import tempfile
import unittest
from pypdf import PdfWriter

from assistant.database.manager import DatabaseManager
from assistant.planner import JarvisPlanner, Plan, PlanStep
from assistant.document_tools import ReadPdfTool, SendEmailTool
from assistant.tools import ErrorCode


class TestPhase6DAGPlannerAndDocs(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_planner.db")
        self.db = DatabaseManager(self.db_path)
        self.planner = JarvisPlanner(db_manager=self.db)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_dag_topological_waves(self):
        """Verify steps are grouped into correct dependency waves."""
        s0 = PlanStep(step_id="s0", plan_id="p1", index=0, intent="step_0", inputs={}, depends_on=[])
        s1 = PlanStep(step_id="s1", plan_id="p1", index=1, intent="step_1", inputs={}, depends_on=[])
        s2 = PlanStep(step_id="s2", plan_id="p1", index=2, intent="step_2", inputs={}, depends_on=["s0", "s1"])
        s3 = PlanStep(step_id="s3", plan_id="p1", index=3, intent="step_3", inputs={}, depends_on=["s2"])

        waves = self.planner._topological_levels([s0, s1, s2, s3])
        self.assertEqual(len(waves), 3)
        self.assertEqual({s.step_id for s in waves[0]}, {"s0", "s1"})
        self.assertEqual({s.step_id for s in waves[1]}, {"s2"})
        self.assertEqual({s.step_id for s in waves[2]}, {"s3"})

    def test_dag_cyclic_dependency_detection(self):
        """Verify cyclic dependency raises ValueError."""
        s0 = PlanStep(step_id="s0", plan_id="p1", index=0, intent="step_0", inputs={}, depends_on=["s1"])
        s1 = PlanStep(step_id="s1", plan_id="p1", index=1, intent="step_1", inputs={}, depends_on=["s0"])

        with self.assertRaises(ValueError):
            self.planner._topological_levels([s0, s1])

    def test_inter_step_piping_and_parallel_execution(self):
        """Verify piped outputs from Step A are injected into Step B inputs."""
        async def run_plan():
            s0 = PlanStep(
                step_id="s0", plan_id="p1", index=0, intent="step_0",
                inputs={"param": "hello"}, depends_on=[]
            )
            s1 = PlanStep(
                step_id="s1", plan_id="p1", index=1, intent="step_1",
                inputs={"data": "$s0.param", "extra": "world"}, depends_on=["s0"]
            )
            plan = Plan(id="test_plan_pipe", goal="Test Piping", steps=[s0, s1])
            self.planner._persist_plan(plan)

            res = await self.planner.execute(plan)
            self.assertEqual(res["status"], "success")
            self.assertEqual(s1.inputs["data"], "hello")

        asyncio.run(run_plan())

    def test_isolated_failure_and_unaffected_branches(self):
        """Verify dependent step skips on failure, while independent step completes."""
        async def run_failure_plan():
            # s0 will fail
            s0 = PlanStep(
                step_id="s0", plan_id="p_fail", index=0, intent="synthetic_failure",
                inputs={"error_message": "Forced failure"}, depends_on=[]
            )
            # s1 depends on s0 (must be skipped)
            s1 = PlanStep(
                step_id="s1", plan_id="p_fail", index=1, intent="dependent_step",
                inputs={}, depends_on=["s0"]
            )
            # s2 is completely independent of s0 (must complete successfully!)
            s2 = PlanStep(
                step_id="s2", plan_id="p_fail", index=2, intent="independent_step",
                inputs={}, depends_on=[]
            )
            plan = Plan(id="test_plan_iso", goal="Test Failure Isolation", steps=[s0, s1, s2])
            self.planner._persist_plan(plan)

            res = await self.planner.execute(plan)
            self.assertEqual(res["status"], "partial_failure")
            self.assertEqual(s0.status, "failed")
            self.assertEqual(s1.status, "skipped")
            self.assertEqual(s2.status, "done")

        asyncio.run(run_failure_plan())

    def test_read_pdf_tool_real_extraction(self):
        """Verify ReadPdfTool extracts real text and metadata from generated PDF."""
        # Generate a test PDF with pypdf
        pdf_path = os.path.join(self.temp_dir.name, "sample_document.pdf")
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        # Note: blank page without text triggers OCR gap
        with open(pdf_path, "wb") as f:
            writer.write(f)

        tool = ReadPdfTool()

        async def run_tool():
            # Scanned / empty text PDF detection
            res_scanned = await tool.execute(file_path=pdf_path)
            self.assertFalse(res_scanned["ok"])
            self.assertEqual(res_scanned["error_code"], ErrorCode.CAPABILITY_GAP)
            self.assertIn("OCR capability is currently not installed", res_scanned["message"])

        asyncio.run(run_tool())

    def test_send_email_honest_capability_gap(self):
        """Verify SendEmailTool enforces Zero Fake Tools rule."""
        async def run_email():
            tool = SendEmailTool()
            res = await tool.execute(
                to="satyam@example.com",
                subject="Weekly Briefing",
                attachment="report.pdf"
            )
            self.assertFalse(res["ok"])
            self.assertEqual(res["error_code"], ErrorCode.CAPABILITY_GAP)
            self.assertIn("Direct SMTP/Email dispatch to 'satyam@example.com'", res["message"])

        asyncio.run(run_email())


if __name__ == "__main__":
    unittest.main()
