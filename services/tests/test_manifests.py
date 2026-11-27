"""k8s/services.generated.yaml must match the service list in fakeco/config.py."""
import pathlib, subprocess, sys

RENDER = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "render_services.py"


def test_generated_manifests_are_up_to_date():
    done = subprocess.run([sys.executable, str(RENDER), "--check"], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr + "\nrun: python3 scripts/render_services.py"
