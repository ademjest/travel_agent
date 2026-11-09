import ast
from pathlib import Path
import unittest

from scripts.audit_prompt_assets import audit, RUNTIME_DIRS, ROOT


class PromptInventoryTests(unittest.TestCase):
    def test_recursive_inventory_binds_every_business_context_and_separates_technical_calls(self):
        result = audit()
        self.assertEqual(result['runtime_file_count'], sum(len(list((ROOT/folder).rglob('*.py'))) for folder in RUNTIME_DIRS))
        contexts = result['model_contexts']
        business = [row for row in contexts if row['category']=='business']
        technical = [row for row in contexts if row['category']=='technical']
        stages = {row['stage_id'] for row in result['registry']['stages']}
        self.assertEqual(len(stages), 20)
        self.assertEqual({row['stage_id'] for row in business}, stages)
        self.assertEqual(len(business), len(stages)+1)  # Includes the explicit summary repair context.
        self.assertEqual(len(technical), 2)
        self.assertTrue(all(not row['stage_id'] for row in technical))
        self.assertTrue(any(row['path'].startswith('tools/research/') for row in business))
        self.assertTrue(any(row['path'].startswith('app/') for row in business))

    def test_text_references_exist_and_never_load_resources_as_text(self):
        from infrastructure.skill_manager import get_skill_manager
        snapshot = get_skill_manager().snapshot()
        for folder in RUNTIME_DIRS:
            for path in (ROOT/folder).rglob('*.py'):
                for call in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                            and call.func.id=='skill_text' and len(call.args)==2
                            and all(isinstance(arg,ast.Constant) for arg in call.args)):
                        stage, block = [arg.value for arg in call.args]
                        self.assertTrue(snapshot.assets[stage].text(block))
