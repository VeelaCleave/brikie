# Running brikie inside OpenShell

[OpenShell](https://github.com/NVIDIA/OpenShell) is a sandboxed runtime
for autonomous agents — managed credentials, policy-controlled network,
rerouted inference. brikie is built to behave well inside it:

- **Credentials via env vars.** Build Sets reference keys as
  `env:ANTHROPIC_API_KEY` etc., so OpenShell's provider injection works
  unmodified: `openshell provider create --type anthropic --from-existing`.
- **Inference rerouting.** brikie's provider presets resolve
  `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` at startup when set, so
  `openshell inference set` routing applies without any brikie config.
- **No-wizard boot.** `brikie --preset anthropic` (or `openai`, …)
  configures the provider entirely from the environment — nothing
  interactive required, nothing written outside the sandbox.

## Bring-your-own-container (works today)

From a checkout of this repository:

```sh
openshell sandbox create --from . -- brikie --preset anthropic
```

(The repo-root `Dockerfile` is the same one behind the official
`ghcr.io/veelacleave/brikie` image.)

## Or use the published image

```sh
docker pull ghcr.io/veelacleave/brikie:latest
```

Compose your brick stack at [brikie.co](https://brikie.co) — the
"run isolated (Docker)" option generates the full jail invocation.
