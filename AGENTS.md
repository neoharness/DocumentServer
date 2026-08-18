# neoHarness Office repository guide

neoHarness Office is a permanent fork of Euro-Office. Euro-Office AI governance
policies and adjacent EU-regulatory policy documents do not apply to this
project; do not add `AI_POLICY.md` or reintroduce those policy documents in
this fork.

## Repository scope

This repository orchestrates the document server and its pinned submodules:
`core-fonts`, `core`, `sdkjs`, `sdkjs-forms`, `web-apps`, `server`,
`dictionaries`, `document-formats`, `document-server-integration`,
`document-server-package`, and `document-templates`.

neoHarness-specific sandbox helpers, finalizer provenance, release tooling,
tests, and operator documentation live under `neoharness/`. Keep those changes
isolated from upstream submodule contents unless a task explicitly requires a
submodule change.

## Dependency boundaries

The build dependency chain flows in one direction:

```text
core-fonts -> core/AllFontsGen -> sdkjs/AllFonts.js -> web-apps -> server -> runtime image
```

- Read a submodule's local technical instructions before changing that submodule.
- Do not update submodule references manually; use the repository's pinned update workflow.
- Do not introduce a root `package.json` into submodules that intentionally have none.
- Validate the full downstream chain after changing build order, submodule
  pins, fonts, SDK output, or `docker-bake.hcl`.
- Build targets can rewrite generated locale and font assets. Commit only intentional generated changes.

## Development server

The development environment in `develop/` runs the server from a prebuilt image
and bind-mounts this checkout at `/develop`.

```sh
cd develop
docker compose pull eo
docker compose up -d eo
curl -sf http://localhost:8080/healthcheck
```

Build individual components inside the running container:

```sh
docker compose exec -T eo make web-apps
docker compose exec -T eo make web-apps-dev
docker compose exec -T eo make sdkjs
docker compose exec -T eo make core
docker compose exec -T eo make server/docservice
```

Use `develop/eo.sh` for isolated named instances:

```sh
cd develop
./eo.sh up <name>
./eo.sh build <name> <target...>
./eo.sh exec <name> <command...>
./eo.sh logs <name>
./eo.sh down <name>
```

Confirm the editor actually opens a document; an HTTP 200 from the shell alone
is not sufficient. Inspect `/var/log/euro-office/documentserver/` inside the
container when conversion or document loading fails.

## neoHarness runtime

- Runtime source: `neoharness/runtime/`
- Release source and verification: `neoharness/release/`
- Canonical build instructions: `neoharness/release/REPRODUCE.md`
- Runtime contract and available operations: `neoharness/runtime/CAPABILITIES.md`
- Publication qualification policy: `neoharness/runtime/quality-policy.v1.json`

Run the focused runtime tests after changing helpers, attestation, paths, rendering, or qualification:

```sh
python3 -m unittest discover -s neoharness/runtime/tests -p 'test_*.py'
python3 -m compileall -q neoharness/runtime/python
git diff --check
```

Final document operations must remain bounded to `/workspace`, run as the
unprivileged sandbox user, validate produced artifacts, and emit provenance
bound to the exact output hash. Keep arbitrary network access out of document
renderers; local workspace files and data URIs are the supported asset boundary.

## Change discipline

- Keep changes focused and avoid unrelated generated-file churn.
- Follow the local style; use spaces, LF line endings, and lines no longer than 120 characters where practical.
- Add focused unit tests for new helpers and regression tests for fixed defects.
- Use Conventional Commits for commit subjects.
- Never add `Signed-off-by` on another person's behalf.
