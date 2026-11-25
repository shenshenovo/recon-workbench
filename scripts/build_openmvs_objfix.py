"""Build a local TextureMesh OBJ-index fix using an existing static OpenMVS build.

The original source, archive and executable remain unchanged. All generated
files, the original executable backup and provenance go into --output.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def build(source, output):
    source, output = source.resolve(strict=True), output.resolve()
    original_build = source / 'make'
    cache = (original_build / 'CMakeCache.txt').read_text()
    old_root = re.search(r'^CMAKE_HOME_DIRECTORY:INTERNAL=(.+)$', cache, re.M).group(1)
    compiler = re.search(r'^CMAKE_CXX_COMPILER:FILEPATH=(.+)$', cache, re.M).group(1)
    flags = (original_build / 'libs/MVS/CMakeFiles/MVS.dir/flags.make').read_text()
    cpp = source / 'libs/MVS/Mesh.cpp'
    text = cpp.read_text()
    old = 'const auto texIdx = faceTexindices[idxFace];'
    new = 'const auto texIdx = faceTexindices.empty() ? 0 : faceTexindices[idxFace];'
    if text.count(old) != 1 or 'bool Mesh::SaveOBJ' not in text:
        raise ValueError('Source does not match the reviewed OpenMVS OBJ exporter; refusing to patch.')
    output.mkdir(parents=True, exist_ok=False)
    (output / 'bin').mkdir()
    original = original_build / 'bin/TextureMesh'
    shutil.copy2(original, output / 'TextureMesh.original')
    shutil.copy2(source / 'LICENSE', output / 'LICENSE.openMVS')
    patched = output / 'Mesh.cpp'
    patched.write_text(text.replace(old, new))
    obj = output / 'Mesh.cpp.o'
    archive = output / 'libMVS.a'
    executable = output / 'bin/TextureMesh'
    arguments = []
    for key in ('CXX_DEFINES', 'CXX_INCLUDES', 'CXX_FLAGS'):
        value = re.search(r'^' + key + r' = (.*)$', flags, re.M).group(1)
        arguments.extend(shlex.split(value.replace(old_root, str(source))))
    compile_command = [compiler, *arguments, '-I' + str(cpp.parent),
                       '-c', str(patched), '-o', str(obj)]
    link_directory = original_build / 'apps/TextureMesh'
    original_link = shlex.split((link_directory / 'CMakeFiles/TextureMesh.dir/link.txt').read_text())
    link_command = []
    for index, value in enumerate(original_link):
        if index and original_link[index - 1] == '-o':
            value = str(executable)
        elif value == '../../lib/libMVS.a':
            value = str(archive)
        elif not value.startswith('-') and not Path(value).is_absolute():
            value = str((link_directory / value).resolve())
        link_command.append(value.replace(old_root, str(source)))
    with (output / 'build.log').open('w') as log:
        print('Compiling the patched Mesh.cpp', flush=True)
        subprocess.run(compile_command, check=True, stdout=log, stderr=subprocess.STDOUT)
        shutil.copy2(original_build / 'lib/libMVS.a', archive)
        subprocess.run(['ar', 'r', str(archive), str(obj)], check=True, stdout=log, stderr=subprocess.STDOUT)
        print('Linking an independent TextureMesh executable', flush=True)
        subprocess.run(link_command, check=True, stdout=log, stderr=subprocess.STDOUT)
    report = {'source': str(source), 'original_source_sha256': sha256(cpp),
              'patched_source_sha256': sha256(patched),
              'original_executable_sha256': sha256(original),
              'patched_executable_sha256': sha256(executable),
              'original_archive_sha256': sha256(original_build / 'lib/libMVS.a'),
              'compile_command': compile_command, 'link_command': link_command,
              'fix': {'before': old, 'after': new},
              'upstream_issue': 'https://github.com/cdcseacave/openMVS/issues/1132'}
    (output / 'build-manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    print(executable, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=Path('.tools/openmvs-objfix'))
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == '__main__':
    main()
