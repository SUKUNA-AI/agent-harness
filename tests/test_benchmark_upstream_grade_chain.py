"""Run native upstream evaluator producers with contained tiny boundaries."""
from pathlib import Path
import unittest
import test_benchmark_upstream as fixtures
from agent_harness import evaluation as ev, strict_yaml
from agent_harness.benchmark_upstream_evaluator import evaluate_upstream
from agent_harness.core import HarnessError


class UpstreamGradeChainTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ProfileFixture();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)

    def prepare(self,suite):
        plan,value,receipt,legacy=self.fixture.materialize(suite)
        if suite!='terminal-bench-2.1':(Path(value['source_root'])/'calc.py').write_text('VALUE = 42\n')
        path=receipt.parent/'input-v2.json'
        ev.write_grade_input(path,evidence_root=self.fixture.root,manifest_digest=value['manifest_digest'],
            task_id=plan['task_id'],trial_id=value['trial_id'],source_root=value['source_root'],
            evaluator_bytes=(self.fixture.assets/plan['evaluator']['entrypoint']['path']).read_bytes(),
            toolchain_bytes=b'contained fixture toolchain',bundle_digest=value['plan_digest'],solve_digest='a'*64,solve_outcome='completed')
        return plan,value,receipt,path

    def test_actual_upstream_producers_bind_generated_inputs_and_execution(self):
        for suite in ev.SUITES[:3]:
            with self.subTest(suite=suite):
                plan,value,receipt,path=self.prepare(suite)
                boundary=fixtures.FixtureEvaluator()
                grade=evaluate_upstream(receipt,path,self.fixture.assets,receipt_digest=fixtures._hash(receipt.read_bytes()),boundary=boundary)
                self.assertEqual(grade['schema_version'],2);self.assertEqual(grade['outcome'],'accepted')
                self.assertEqual(ev.verify_execution_grade(grade,self.fixture.root),'verified_subject_chain')
                inputs=ev._evidence_record(grade['evaluator_input'],self.fixture.root,ev.EVALUATOR_INPUT)
                if suite!='terminal-bench-2.1':self.assertIn('generated',{row['role'] for row in inputs['artifacts']})
                self.assertEqual(len(boundary.calls),1)

    def test_upstream_candidate_drift_during_boundary_never_produces_grade(self):
        plan,value,receipt,path=self.prepare('swe-bench-verified')
        class Drifting(fixtures.FixtureEvaluator):
            def run(self,command,**options):
                result=super().run(command,**options)
                (Path(value['source_root'])/'calc.py').write_text('VALUE = 13\n')
                return result
        with self.assertRaisesRegex(HarnessError,'CANDIDATE_DRIFT'):
            evaluate_upstream(receipt,path,self.fixture.assets,receipt_digest=fixtures._hash(receipt.read_bytes()),boundary=Drifting())
        self.assertFalse((self.fixture.root/'grading'/value['trial_id']/'execution.json').exists())


if __name__=='__main__':unittest.main()
