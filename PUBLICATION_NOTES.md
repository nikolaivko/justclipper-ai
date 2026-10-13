# Publication preparation

This snapshot was copied from the current justClipper checkout. The original checkout was read only during preparation. Current application changes and tests were included without importing Git history.

## Excluded

- `.env` and private configuration.
- OAuth tokens and sessions, browser cookies and credential files.
- All existing `data/`, video sources, renders, job metadata and databases.
- Logs, Python bytecode, test caches, temporary files and downloaded models.
- Virtual environments, local binaries and third-party tool checkouts.
- The original `.git` directory and its commit history.
- One-off root debugging and patch scripts, including browser cookie extraction helpers.

The Stories development module is included under `tools/narrated-dev/` as source only. Its environment, cookies, voice profiles, recordings, generated media, models and third-party Voicebox checkout are excluded. Configure its separate `.env` and install dependencies before launching it; the repository README describes the setup.

## Configuration

The API integrations remain in the application. API keys and OAuth credentials are read from the user's environment. `.env.example` contains blank credential values. Public Google API endpoints and test-only dummy strings are not private credentials.

The production OAuth fallback URL was replaced with localhost in this snapshot. Configure your own redirect URL before deploying.

One publishing test fixture was corrected in this snapshot to include the `#Shorts` suffix that the publisher adds to descriptions. The publisher behavior was preserved.

## Checks

Preparation checks compare the included source with credential values from the private configuration in memory, scan common credential formats, check excluded file types and symlinks, and verify the original source hashes. Private credential values are not written to the report.

Automated checks have limits: they cannot establish that a key was never exposed in an older repository or revoke an existing credential. This archive intentionally contains no old Git history.

## Next steps

Extract the archive and upload the contents of `justclipper/` to a new GitHub repository. Keep your production checkout separate. Select a source license deliberately; no new license was added by the preparation process.
