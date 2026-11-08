# Contributing

All changes to `main` should be proposed through a pull request. Create a feature branch, make focused changes, and open a PR targeting `main`. Do not push commits directly to `main`.

The GitHub Actions **CI** workflow checks backend tests and the frontend production build on pull requests and pushes to `main`. Resolve failures before merging. Review the diff and include any required migration or configuration notes in the PR description.

## Local checks

```bash
cd backend && pytest -q
cd ../frontend && npm ci && npm run build
```
