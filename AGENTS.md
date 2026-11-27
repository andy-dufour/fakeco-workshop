# Agent guidance

- Deliver work as a branch and a pull request against `main`. Never push to `main`.
- Run `FAKECO_LOCAL=1 python -m pytest tests -q` in `services/` before you open a pull request, and say in the description what you ran.
- Keep a change small and about one thing. Prefer what the code already has (a backend route, a tool definition, a helper) over writing a new one.
- Update `SPEC.md` or `services/INTERNALS.md` when behavior or the API changes.
- Do not hand-edit `k8s/services.generated.yaml`; run `scripts/render_services.py`.
- A merge to `main` is deployed to the cluster.
