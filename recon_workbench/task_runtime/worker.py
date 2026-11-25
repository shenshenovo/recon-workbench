"""执行握手包装器：未收到命令前不启动任何算法子进程。"""
import json
import subprocess
import sys


def main():
    line = sys.stdin.buffer.readline(1024 * 1024)
    if not line:
        return 1
    command = json.loads(line)
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        return 1
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    return process.wait()


if __name__ == '__main__':
    sys.exit(main())
