// Keep the newest checkpoint and its predecessor, replacing only the older
// slot. An interrupted upload therefore leaves the last good checkpoint.
const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const file = path.resolve(process.argv[2]);
  const state = JSON.parse(fs.readFileSync(file, 'utf8'));
  const artifact = require(process.env.SUMIRE_ARTIFACT_CLIENT);
  const client = artifact.default || new artifact.DefaultArtifactClient();
  const name = `sumire-repair-state-v1-${process.env.GITHUB_RUN_ID}-${process.env.GITHUB_RUN_ATTEMPT}-${state.revision % 2}`;
  try {
    await client.deleteArtifact(name);
  } catch (error) {
    if (!(error instanceof artifact.ArtifactNotFoundError)) throw error;
  }
  // Only two live artifacts per job, independent of the number of repairs.
  await client.uploadArtifact(name, [file], path.dirname(file), {retentionDays: 14});
}

main().catch(() => {
  console.error('Could not persist repair checkpoint; stopping before further API calls.');
  process.exitCode = 1;
});
