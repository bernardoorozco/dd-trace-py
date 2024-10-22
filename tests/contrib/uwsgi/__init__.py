import os
import subprocess


def run_uwsgi(cmd):
    def _run(*args):
        env = os.environ.copy()
        for k, v in env.items():
            if k.startswith("DD_"):
                print(k, v)
        return subprocess.Popen(cmd + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

    return _run
