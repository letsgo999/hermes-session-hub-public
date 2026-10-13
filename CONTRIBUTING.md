# Contributing

1. Fork, then branch from `main` as `feature/<name>` or `fix/<name>`.
2. Change product code together with tests. Do not add runtime data, reports, logs or personal paths.
3. Before committing run:

   ```bash
   cd mvp
   python tests/run_python_tests.py
   npm run test:node
   python build.py
   ```

4. Open a pull request describing the purpose, the verification commands you ran, and any
   user-visible or install impact.
5. Releases: bump `plugins/session-hub/plugin.yaml`, `dashboard/manifest.json` and `package.json`,
   update `CHANGELOG.md`, build, and attach `dist/` as `session-hub-<version>.zip` to a `v<version>` tag.
