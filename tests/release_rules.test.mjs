// What each pull request title releases: .releaserc.json's rules run through the pinned commit-analyzer.
// CI's check job runs this with `node --test tests/`; pytest does not collect it.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { analyzeCommits } from "@semantic-release/commit-analyzer";

const config = JSON.parse(readFileSync(new URL("../.releaserc.json", import.meta.url), "utf8"));
const [, analyzerConfig] = config.plugins.find(
  (plugin) => Array.isArray(plugin) && plugin[0] === "@semantic-release/commit-analyzer",
);
const quiet = { log() {}, error() {}, success() {}, warn() {} };

const cases = [
  ["feat!: drop a flag", "minor"], // breaking changes raise the minor version while on 0.x
  ["fix(gate)!: refuse a header", "minor"],
  ["fix: handle an empty list\n\nBREAKING CHANGE: the list is required", "minor"],
  ["feat: add a flag", "minor"],
  ["fix: handle an empty list", "patch"],
  ["perf: cache the parse", "patch"],
  ["build(deps): bump a runtime dependency", "patch"],
  ["build(deps-dev): bump a development dependency", null],
  ["ci(deps): bump an action", null],
  ["chore: tidy", null],
  ["docs: explain the gate", null],
];

// The version lives only in the tag: the release commits CHANGELOG.md and nothing else back to the branch (no
// manifest carries a version), marks that commit [skip ci], and publishes its notes on the GitHub Release too.
test("a release commits only CHANGELOG.md and publishes its notes on the GitHub Release", () => {
  const names = config.plugins.map((plugin) => (Array.isArray(plugin) ? plugin[0] : plugin));
  const options = (name) => {
    const plugin = config.plugins.find((entry) => Array.isArray(entry) && entry[0] === name);
    assert.ok(plugin, `${name} is configured with options`);
    return plugin[1];
  };
  assert.ok(!names.includes("@semantic-release/npm"), "@semantic-release/npm would commit a version to package.json");
  assert.equal(options("@semantic-release/changelog").changelogFile, "CHANGELOG.md");
  const git = options("@semantic-release/git");
  assert.deepEqual(git.assets, ["CHANGELOG.md"]);
  assert.match(git.message, /^chore\(release\): \$\{nextRelease\.version\} \[skip ci\]/);
  // The changelog is written (prepare) before the git plugin commits it (prepare, in plugin order).
  assert.ok(names.indexOf("@semantic-release/changelog") < names.indexOf("@semantic-release/git"));
  assert.ok(names.includes("@semantic-release/release-notes-generator"));
  assert.ok(names.includes("@semantic-release/github"));
});

for (const [message, expected] of cases) {
  test(`${JSON.stringify(message.split("\n")[0])} releases ${expected ?? "nothing"}`, async () => {
    const release = await analyzeCommits(analyzerConfig, {
      commits: [{ hash: "0000000", message }],
      logger: quiet,
      cwd: process.cwd(),
    });
    assert.equal(release, expected);
  });
}
