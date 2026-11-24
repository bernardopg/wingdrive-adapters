# Registry signing keys

`index.json` is signed with [minisign](https://jedisct1.github.io/minisign/) (Ed25519). Every
WingDrive build ships both public keys below and accepts an index signed by either one.

| File | Role | Private key |
|---|---|---|
| `active.pub` | Signs every published index | `MINISIGN_SECRET_KEY` in the `registry-publish` environment, plus an offline backup with the maintainer |
| `standby.pub` | Takes over if the active key is lost or leaked | Offline only, with the maintainer; never in CI |

The maintainer keeps both private keys in a `.keys/` folder of their checkout, which `.gitignore`
excludes, and a backup in their password manager.

Verify a published index:

```bash
minisign -V -p keys/active.pub -m index.json
```

## Rotation

1. Move the standby private key into the `registry-publish` environment and rename the files:
   `standby.pub` becomes `active.pub`.
2. Generate a new standby key offline and commit its public key as `standby.pub`.
3. Ship the new pair in the next WingDrive release. Apps released before it keep working because
   they already trust the key that is now active.
4. If the old key leaked, add every version it signed after the leak to `revoked.json`.

## Log

| Date | Event | Active key id | Standby key id |
|---|---|---|---|
| 2026-11-24 | Keys created | 14BF5DD541151F16 | 7151766592ED92E2 |
