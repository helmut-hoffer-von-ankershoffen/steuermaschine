import subprocess
import sys

from moved_support import TOOLS


def test_train_self_test_passes(tmp_path):
    done = subprocess.run([sys.executable, "-c", "import sys; from steuermaschine_heinzel.native.train_lib import train; sys.exit(train.main(sys.argv[1:]))", "--self-test"], capture_output=True, text=True,
                          timeout=60, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert "SELF-TEST PASSED" in done.stdout


def test_train_rejects_unknown_arguments(tmp_path):
    done = subprocess.run([sys.executable, "-c", "import sys; from steuermaschine_heinzel.native.train_lib import train; sys.exit(train.main(sys.argv[1:]))", "--cancel"], capture_output=True, text=True,
                          timeout=30, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert done.returncode == 2
