import importlib.machinery
import importlib.util
import json
import textwrap
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('helper', str(ROOT / 'scripts/build-plugin'))
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
loader.exec_module(helper)


class SDKTests(unittest.TestCase):
    def test_detection(self):
        for help_text in ('... banquise_agent\n... all\n', 'banquise_agent: phony\nall: phony\n'):
            self.assertEqual(helper.detect('MYSQL_ADD_PLUGIN(\n BANQUISE_AGENT x.cc)', help_text), 'banquise_agent')
        self.assertEqual(helper.detect('MYSQL_ADD_PLUGIN(vmstat x.cc)', '... vmstat'), 'vmstat')
        self.assertEqual(helper.detect('set(foo bar)', '... custom', 'custom'), 'custom')
        for code, target in [('MYSQL_ADD_PLUGIN(nope x.cc)', ''),
                             ('MYSQL_ADD_PLUGIN(a a.cc) MYSQL_ADD_PLUGIN(b b.cc)', ''),
                             ('', 'all'), ('', 'minbuild')]:
            with self.assertRaises(RuntimeError):
                helper.detect(code, '... all\n... minbuild', target)

    def test_workflow_matrix(self):
        workflow = (ROOT / '.github/workflows/build-plugin.yml').read_text()
        script = textwrap.dedent(workflow.split("python3 - <<'PYTHON'\n", 1)[1].split('          PYTHON', 1)[0])
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'output'
            env = dict(os.environ, VERSIONS='11.8.5\n13.0.2\n13.0.2',
                       SDK_IMAGE='quay.io/example/sdk', SDK_IMAGES='{"13.0.2":"quay.io/example/sdk@sha256:abc"}',
                       GITHUB_OUTPUT=str(output))
            subprocess.run(['python3', '-c', script], env=env, check=True)
            matrix = json.loads(output.read_text().split('=', 1)[1])['include']
            self.assertEqual(len(matrix), 2)
            self.assertEqual(matrix[0]['image'], 'quay.io/example/sdk:11.8.5')
            self.assertEqual(matrix[1]['image'], 'quay.io/example/sdk@sha256:abc')
            for invalid in ('11.8.x', 'main', '', '[13.0]', '13.0.2;echo nope'):
                env['VERSIONS'] = invalid
                result = subprocess.run(['python3', '-c', script], env=env, capture_output=True)
                self.assertNotEqual(result.returncode, 0, invalid)

    def test_new_workflow_matrix(self):
        workflow = (ROOT / '.github/workflows/built-plugin-new.yml').read_text()
        script = textwrap.dedent(workflow.split("python3 - <<'PYTHON'\n", 1)[1].split('          PYTHON', 1)[0])
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'output'
            env = dict(os.environ, VERSIONS='1234 11.4 11.8.2 11.8.2', GITHUB_OUTPUT=str(output))
            subprocess.run(['python3', '-c', script], env=env, check=True)
            matrix = json.loads(output.read_text().split('=', 1)[1])['include']
            self.assertEqual(matrix, [
                {'mariadb_version': '1234'},
                {'mariadb_version': '11.4'},
                {'mariadb_version': '11.8.2'},
            ])

            env['VERSIONS'] = '[1234, "11.4", "11.8.2"]'
            subprocess.run(['python3', '-c', script], env=env, check=True)
            matrix = json.loads(output.read_text().splitlines()[-1].split('=', 1)[1])['include']
            self.assertEqual(matrix, [
                {'mariadb_version': '1234'},
                {'mariadb_version': '11.4'},
                {'mariadb_version': '11.8.2'},
            ])

            for invalid in ('', '11.8.x', 'main', '[13.0]', '[true]', '[{}]', '13.0.2;echo nope'):
                env['VERSIONS'] = invalid
                result = subprocess.run(['python3', '-c', script], env=env, capture_output=True)
                self.assertNotEqual(result.returncode, 0, invalid)

    def test_real_cmake_plugin_only_and_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            server = base / 'server'
            plugin = base / 'source'
            plugin.mkdir()
            (server / 'plugin').mkdir(parents=True)
            (plugin / 'CMakeLists.txt').write_text('MYSQL_ADD_PLUGIN(BANQUISE_AGENT plugin.c)\n')
            (plugin / 'plugin.c').write_text('int plugin_function(void) { return 42; }\n')
            for doc in ('README.md', 'LICENSE'):
                (plugin / doc).write_text('fixture documentation\n')
            (server / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.10)
project(fixture C)
function(MYSQL_ADD_PLUGIN name source)
  string(TOLOWER "${name}" target)
  add_library(${target} MODULE ${source})
  set_target_properties(${target} PROPERTIES PREFIX "")
endfunction()
add_custom_target(server_forbidden ALL COMMAND ${CMAKE_COMMAND} -E false)
if(EXISTS "${CMAKE_SOURCE_DIR}/plugin/source/CMakeLists.txt")
  add_subdirectory(plugin/source)
endif()
''')
            output = base / 'output'
            env = dict(os.environ, MARIADB_SOURCE=str(server), PLUGIN_SOURCE=str(plugin),
                       GITHUB_OUTPUT=str(output), CCACHE_DIR=str(base / 'ccache'))
            subprocess.run(['cmake', '-S', str(server), '-B', str(server / 'build')],
                           env=env, check=True, stdout=subprocess.DEVNULL)
            subprocess.run([str(ROOT / 'scripts/build-plugin'), 'https://example.org/source.git'], env=env, check=True)
            library = dict(line.split('=', 1) for line in output.read_text().splitlines())['path']
            env.update(GITHUB_WORKSPACE=str(base), PLUGIN_NAME='banquise_agent', PLUGIN_LIBRARY='banquise_agent.so',
                       PLUGIN_PATH=library, PACKAGE_VERSION='v1.0', MARIADB_VERSION='13.0.2')
            subprocess.run([str(ROOT / 'scripts/package-plugin')], env=env, check=True)
            archive = next((base / 'dist').glob('*.tar.gz'))
            with tarfile.open(archive) as tar:
                paths = tar.getnames()
                for suffix in ('lib/mariadb/plugin/banquise_agent.so', 'share/doc/banquise_agent/README.md',
                               'share/doc/banquise_agent/LICENSE', 'INSTALL.txt'):
                    self.assertTrue(any(path.endswith('/' + suffix) for path in paths), suffix)
            subprocess.run(['sha256sum', '--check', archive.name + '.sha256'], cwd=archive.parent, check=True)

            # Exercise the local URL mode, including an actual fast-forward update.
            def git(*args):
                return subprocess.run(['git', '-C', str(plugin)] + list(args),
                                      check=True, capture_output=True, text=True).stdout.strip()
            git('init', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            git('add', '.')
            git('commit', '-m', 'initial')
            (server / 'plugin/source').unlink()
            env.pop('PLUGIN_SOURCE')
            subprocess.run([str(ROOT / 'scripts/build-plugin'), str(plugin)], env=env, check=True)
            (plugin / 'plugin.c').write_text('int plugin_function(void) { return 43; }\n')
            git('add', '.')
            git('commit', '-m', 'update')
            subprocess.run([str(ROOT / 'scripts/build-plugin'), str(plugin)], env=env, check=True)
            head = subprocess.check_output(['git', '-C', str(server / 'plugin/source'), 'rev-parse', 'HEAD'], text=True).strip()
            self.assertEqual(head, git('rev-parse', 'HEAD'))


if __name__ == '__main__':
    unittest.main()
