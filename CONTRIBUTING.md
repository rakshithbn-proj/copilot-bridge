# Contributing to Copilot Bridge

Thank you for your interest! This document covers how to build, test, and submit changes.

## Project structure

```
copilot-bridge/
├── copilot-bridge-extension/   VS Code extension (TypeScript)
│   ├── src/extension.ts        All server logic
│   ├── package.json
│   └── tsconfig.json
└── copilot-bridge-dist/        Python client library
    ├── copilot_bridge.py       CopilotBridge + CopilotAgent classes
    ├── copilot_bridge.pyi      Type stubs
    └── setup.py
```

## Development setup

### Extension

```bash
cd copilot-bridge-extension
npm install
npm run watch       # incremental TypeScript compilation
```

Open the repo in VS Code, press **F5** to launch an Extension Development Host with the bridge running.

### Python client

```bash
cd copilot-bridge-dist
pip install -e .    # editable install
```

## Running the smoke tests

With the extension running in a VS Code instance:

```bash
cd copilot-bridge-extension
python test_copilot_bridge_smoke.py          # all tests (including LLM)
python test_copilot_bridge_smoke.py --no-llm # skip LLM-dependent tests
```

## Building distributable artifacts

**VSIX (VS Code extension package):**

```bash
cd copilot-bridge-extension
npm run package
```

**Python wheel:**

```bash
cd copilot-bridge-dist
pip install build
python -m build
# wheel → dist/copilot_bridge-x.x.x-py3-none-any.whl
```

Or run `build_whl.bat` on Windows.

## Submitting a pull request

1. Fork the repo and create a branch: `git checkout -b feature/my-change`
2. Make your changes with appropriate tests
3. Ensure the smoke test suite passes
4. Open a PR with a clear description of the change and motivation

## Releasing a new version

1. Update the version number in all four places (see **Versioning** below)
2. Commit: `git commit -m "chore: bump version to x.y.z"`
3. Tag and push:
   ```bash
   git tag v5.2.0
   git push origin main --tags
   ```
4. GitHub Actions `release.yml` triggers automatically and:
   - Publishes the VSIX to the VS Code Marketplace (requires `VSCE_PAT` secret)
   - Publishes the wheel to PyPI (requires PyPI Trusted Publisher configured)

## Versioning

Both components share the same version number. Update it in:
- `copilot-bridge-extension/package.json` → `"version"`
- `copilot-bridge-dist/setup.py` → `version=`
- `copilot-bridge-dist/copilot_bridge.py` → `VERSION = `
- `copilot-bridge-extension/src/extension.ts` → `let extVersion =`

## Code style

- TypeScript: follow the existing patterns in `extension.ts`; no linter enforced yet
- Python: PEP 8; type hints on all public methods

## License

By contributing you agree that your contributions will be licensed under the MIT License.
