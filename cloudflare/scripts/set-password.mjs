#!/usr/bin/env node
// Prints SQL that sets a Workers-verifiable (<=100k iteration) werkzeug-format
// PBKDF2 hash for a user. Werkzeug (the Flask app) can verify it too.
// Usage: node scripts/set-password.mjs <username> [--create-admin]  (password read from MD_PASSWORD env or stdin)
// Then: npx wrangler d1 execute <DB> --remote --file out.sql   (or --local)
import { pbkdf2Sync, randomBytes } from 'node:crypto';
const [username, flag] = process.argv.slice(2);
if (!username) { console.error('usage: set-password.mjs <username> [--create-admin]'); process.exit(2); }
let password = process.env.MD_PASSWORD;
if (!password) password = (await new Promise((r) => { let d = ''; process.stdin.on('data', (c) => (d += c)).on('end', () => r(d)); })).replace(/\r?\n$/, '');
if (!password) { console.error('empty password'); process.exit(2); }
const iterations = 100000;
const alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
const salt = [...randomBytes(16)].map((b) => alphabet[b % 62]).join('');
const hash = `pbkdf2:sha256:${iterations}$${salt}$${pbkdf2Sync(password, salt, iterations, 32, 'sha256').toString('hex')}`;
const q = (s) => `'${s.replace(/'/g, "''")}'`;
const ver = 1000 + Math.floor(Math.random() * 1e9);
if (flag === '--create-admin') {
  console.log(`INSERT INTO user (username, password_hash, role, can_download, can_view_adult, session_version) VALUES (${q(username)}, ${q(hash)}, 'admin', 1, 1, ${ver})
  ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash, session_version = excluded.session_version;`);
} else {
  console.log(`UPDATE user SET password_hash = ${q(hash)}, session_version = ${ver} WHERE username = ${q(username)};`);
}
