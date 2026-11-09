"""Exact final-byte visibility controls; fixtures do not establish isolation."""
import hashlib
import json
import unittest
from unittest.mock import patch

import test_task_flow_experiment as fixtures
from agent_harness import task_flow_experiment as flow
from agent_harness._vendor import yaml
from agent_harness.core import HarnessError, canonical, digest


class TaskFlowVisibilityTests(unittest.TestCase):
    setUp = fixtures.TaskFlowPreparationTests.setUp
    git = fixtures.TaskFlowPreparationTests.git
    put = fixtures.TaskFlowPreparationTests.put
    document = fixtures.TaskFlowPreparationTests.document
    prepare = fixtures.TaskFlowPreparationTests.prepare

    def hidden(self, raw, name="private/collision"):
        task = self.spec['tasks'][0]
        evaluator = self.document(task['hidden_evaluator'])
        evaluator['files'].append(self.put(name, raw))
        task['hidden_evaluator'] = self.put(task['id']+'/evaluator.json', evaluator)

    def rewrite_receipt(self, receipt):
        content = dict(receipt); content.pop('preparation_digest')
        receipt['preparation_digest'] = digest(content)
        (self.output/'preparation.json').write_bytes(canonical(receipt))

    def test_pretty_dag_hidden_equals_final_canonical_bytes(self):
        task = self.spec['tasks'][0]
        dag = self.document(task['shared_decomposition'])
        task['shared_decomposition'] = self.put('pretty-dag.json', json.dumps(dag, indent=2))
        self.hidden(canonical(dag))
        with self.assertRaisesRegex(HarnessError, 'HIDDEN_EVALUATOR_LEAK'):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_generated_arm_manifest_is_part_of_final_visibility(self):
        clean = self.prepare(output=self.root/'clean')
        arm = clean['tasks'][0]['arm_manifests']['A']
        self.hidden((self.root/'clean'/arm['path']).read_bytes())
        with self.assertRaisesRegex(HarnessError, 'HIDDEN_EVALUATOR_LEAK'):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_verify_rejects_self_consistent_cross_visibility_inventory(self):
        receipt = self.prepare()
        raw = (self.output/receipt['tasks'][0]['mission']['path']).read_bytes()
        name = 'evaluator-private/injected.bin'
        (self.output/name).write_bytes(raw)
        receipt['files'].append({'path': name, 'sha256': hashlib.sha256(raw).hexdigest(),
            'bytes': len(raw), 'mode': '100644', 'visibility': 'hidden'})
        self.rewrite_receipt(receipt)
        with self.assertRaisesRegex(HarnessError, 'HIDDEN_EVALUATOR_LEAK'):
            flow.verify(self.output)

    def test_document_encodings_guard_actual_normalized_mission_and_dag(self):
        for field in ('mission', 'shared_decomposition'):
            task = self.spec['tasks'][0]
            original = task[field]
            value = self.document(original)
            for encoding, raw in (('canonical', canonical(value)),
                                  ('pretty', json.dumps(value, indent=2).encode()),
                                  ('yaml', yaml.safe_dump(value, sort_keys=False).encode())):
                with self.subTest(field=field, encoding=encoding):
                    task[field] = self.put('document/'+field, raw)
                    self.assertEqual(flow._document(raw, flow.MISSION if field == 'mission' else flow.DAG), value)
                    self.assertEqual(raw == canonical(value), encoding == 'canonical')
                    self.hidden(canonical(value), 'private/'+field)
                    with self.assertRaisesRegex(HarnessError, 'HIDDEN_EVALUATOR_LEAK'):
                        self.prepare()
                    self.assertFalse(self.output.exists())
            task[field] = original
            # Reset hidden additions before testing the other document kind.
            evaluator = self.document(task['hidden_evaluator'])
            evaluator['files'] = evaluator['files'][:1]
            task['hidden_evaluator'] = self.put(task['id']+'/evaluator.json', evaluator)

    def test_immutable_normalized_paths_visibility_modes_and_exact_duplicates(self):
        for first, second in (('solve', 'hidden'), ('hidden', 'solve')):
            with self.subTest(first=first):
                inventory = flow._MaterialInventory()
                ref = inventory.put('same/path', b'shared', first)
                self.assertEqual(inventory.put('same/path', b'shared', first), ref)
                self.assertEqual(inventory.total_bytes, len(b'shared'))
                with self.assertRaisesRegex(HarnessError, 'VISIBILITY_CONFLICT'):
                    inventory.put('same/path', b'shared', second)
                with self.assertRaisesRegex(HarnessError, 'OUTPUT_COLLISION'):
                    inventory.put('same/path', b'shared', first, '100755')
                with self.assertRaisesRegex(HarnessError, 'OUTPUT_COLLISION'):
                    inventory.put('Same/path', b'shared', first)
                self.assertEqual(inventory.files['same/path'], (b'shared', '100644', first))
                inputs = flow._InputInventory()
                source = {'path': 'same/path', 'sha256': ref['sha256']}
                inputs.add(source, first); inputs.add(source, first)
                with self.assertRaisesRegex(HarnessError, 'VISIBILITY_CONFLICT'):
                    inputs.add(source, second)

    def test_same_original_path_visibility_conflicts_in_both_orders(self):
        ref = self.put('shared/library.py', 'explicitly shared public library')
        first, second = self.spec['tasks'][:2]
        for public_task, hidden_task in ((first, second), (second, first)):
            with self.subTest(public_task=public_task['id']):
                public_task['public_assets'] = [ref]
                evaluator = self.document(hidden_task['hidden_evaluator'])
                evaluator['files'].append(ref)
                hidden_task['hidden_evaluator'] = self.put(hidden_task['id']+'/evaluator.json', evaluator)
                with self.assertRaisesRegex(HarnessError, 'VISIBILITY_CONFLICT'):
                    self.prepare()
                self.assertFalse(self.output.exists())
                public_task['public_assets'] = []
                evaluator['files'].pop()
                hidden_task['hidden_evaluator'] = self.put(hidden_task['id']+'/evaluator.json', evaluator)

    def test_explicit_shared_public_libraries_and_identical_duplicates_are_allowed(self):
        ref = self.put('shared/library.py', 'explicitly shared public library')
        for task in self.spec['tasks']: task['public_assets'] = [ref, ref]
        receipt = self.prepare()
        self.assertEqual(receipt, flow.verify(self.output))
        self.assertEqual(sum(row['path'].endswith('/assets/'+ref['sha256']) for row in receipt['files']), len(self.spec['tasks']))

    def test_verify_rejects_resigned_visibility_metadata_mutations(self):
        receipt = self.prepare()
        for prefix, audience in (('solve/', 'controller'), ('evaluator-private/', 'solve')):
            row = next(row for row in receipt['files'] if row['path'].startswith(prefix))
            original = row['visibility']; row['visibility'] = audience
            self.rewrite_receipt(receipt)
            with self.assertRaisesRegex(HarnessError, 'VISIBILITY_METADATA_CONFLICT'):
                flow.verify(self.output)
            row['visibility'] = original
        self.rewrite_receipt(receipt)
        self.assertEqual(receipt, flow.verify(self.output))

    def test_old_contradictory_bundle_is_rejected_by_independent_verification(self):
        task = self.spec['tasks'][0]
        dag = self.document(task['shared_decomposition'])
        task['shared_decomposition'] = self.put('old-pretty-dag.json', json.dumps(dag, indent=2))
        self.hidden(canonical(dag))
        # Reproduce the old publisher's missing final-byte guard, retaining its
        # unchanged version-1 receipt format and self-consistent digest.
        with patch.object(flow, '_final_visibility'):
            receipt = self.prepare()
        self.assertEqual(receipt['schema_version'], 1)
        with self.assertRaisesRegex(HarnessError, 'HIDDEN_EVALUATOR_LEAK'):
            flow.verify(self.output, expected_digest=receipt['preparation_digest'])

    def test_real_closed_validator_rejects_wrong_inventory_metadata_types(self):
        receipt = self.prepare()
        self.assertEqual(flow._document(canonical(receipt), flow.PREPARATION), receipt)
        row = receipt['files'][0]
        for key, value in (('bytes', True), ('visibility', 'shared'), ('extra', 'forbidden')):
            original = dict(row); row[key] = value
            self.rewrite_receipt(receipt)
            with self.assertRaises(ValueError): flow.verify(self.output)
            row.clear(); row.update(original)

    def test_prepublication_readback_refuses_mutated_files_without_complete_receipt(self):
        original = flow.atomic_write
        def mutate(path, raw, **kwargs):
            result = original(path, raw, **kwargs)
            if path.name == 'mission.json': path.write_bytes(b'mutated during publication')
            return result
        with patch.object(flow, 'atomic_write', side_effect=mutate):
            with self.assertRaisesRegex(HarnessError, 'BYTES_CHANGED'):
                self.prepare()
        self.assertTrue(self.output.exists())
        self.assertFalse((self.output/'preparation.json').exists())
        before = {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob('*') if path.is_file()}
        with self.assertRaisesRegex(HarnessError, 'PARTIAL_PREPARATION'):
            self.prepare()
        self.assertEqual(before, {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob('*') if path.is_file()})


if __name__ == '__main__':
    unittest.main()
